#
# torrent.py
#
# Copyright (C) 2007-2009 Andrew Resch <andrewresch@gmail.com>
#
# Deluge is free software.
#
# You may redistribute it and/or modify it under the terms of the
# GNU General Public License, as published by the Free Software
# Foundation; either version 3 of the License, or (at your option)
# any later version.
#
# deluge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with deluge.    If not, write to:
# 	The Free Software Foundation, Inc.,
# 	51 Franklin Street, Fifth Floor
# 	Boston, MA  02110-1301, USA.
#
#    In addition, as a special exception, the copyright holders give
#    permission to link the code of portions of this program with the OpenSSL
#    library.
#    You must obey the GNU General Public License in all respects for all of
#    the code used other than OpenSSL. If you modify file(s) with this
#    exception, you may extend this exception to your version of the file(s),
#    but you are not obligated to do so. If you do not wish to do so, delete
#    this exception statement from your version. If you delete this exception
#    statement from all source files in the program, then also delete it here.
#

"""Internal Torrent class"""

import os
import time
import logging
import re
from urllib import unquote
from urlparse import urlparse

from twisted.internet.defer import Deferred, DeferredList
from twisted.internet.task import LoopingCall
from deluge._libtorrent import lt

import deluge.common
import deluge.component as component
from deluge.configmanager import ConfigManager, get_config_dir
from deluge.event import *
from deluge.common import decode_string

TORRENT_STATE = deluge.common.TORRENT_STATE

log = logging.getLogger(__name__)

def sanitize_filepath(filepath, folder=False):
    """
    Returns a sanitized filepath to pass to libotorrent rename_file().
    The filepath will have backslashes substituted along with whitespace
    padding and duplicate slashes stripped. If `folder` is True a trailing
    slash is appended to the returned filepath.
    """
    def clean_filename(filename):
        filename = filename.strip()
        if filename.replace('.', '') == '':
            return ''
        return filename

    if '\\' in filepath or '/' in filepath:
        folderpath = filepath.replace('\\', '/').split('/')
        folderpath = [clean_filename(x) for x in folderpath]
        newfilepath = '/'.join(filter(None, folderpath))
    else:
        newfilepath = clean_filename(filepath)

    if folder is True:
        return newfilepath + '/'
    else:
        return newfilepath

class TorrentOptions(dict):
    def __init__(self):
        config = ConfigManager("core.conf").config
        options_conf_map = {
            "max_connections": "max_connections_per_torrent",
            "max_upload_slots": "max_upload_slots_per_torrent",
            "max_upload_speed": "max_upload_speed_per_torrent",
            "max_download_speed": "max_download_speed_per_torrent",
            "prioritize_first_last_pieces": "prioritize_first_last_pieces",
            "sequential_download": "sequential_download",
            "compact_allocation": "compact_allocation",
            "download_location": "download_location",
            "auto_managed": "auto_managed",
            "stop_at_ratio": "stop_seed_at_ratio",
            "stop_ratio": "stop_seed_ratio",
            "remove_at_ratio": "remove_seed_at_ratio",
            "move_completed": "move_completed",
            "move_completed_path": "move_completed_path",
            "add_paused": "add_paused",
            "shared": "shared"
        }
        for opt_k, conf_k in options_conf_map.iteritems():
            self[opt_k] = config[conf_k]
        self["file_priorities"] = []
        self["mapped_files"] = {}

class Torrent(object):
    """Torrent holds information about torrents added to the libtorrent session.
    """
    def __init__(self, handle, options, state=None, filename=None, magnet=None, owner=None):
        # Set the torrent_id for this torrent
        self.torrent_id = str(handle.info_hash())

        if log.isEnabledFor(logging.DEBUG):
            log.debug("Creating torrent object %s", self.torrent_id)

        # Get the core config
        self.config = ConfigManager("core.conf")
        self.rpcserver = component.get("RPCServer")

        # This dict holds previous status dicts returned for this torrent
        # We use this to return dicts that only contain changes from the previous
        # {session_id: status_dict, ...}
        self.prev_status = {}
        self.prev_status_cleanup_loop = LoopingCall(self._cleanup_prev_status)
        self.prev_status_cleanup_loop.start(10)

        # Set the libtorrent handle
        self.handle = handle

        # Keep a list of Deferreds for file indexes we're waiting for file_rename alerts on
        # This is so we can send one folder_renamed signal instead of multiple
        # file_renamed signals.
        # [{index: Deferred, ...}, ...]
        self.waiting_on_folder_rename = []

        # We store the filename just in case we need to make a copy of the torrentfile
        if not filename:
            # If no filename was provided, then just use the infohash
            filename = self.torrent_id

        self.filename = filename

        # Store the magnet uri used to add this torrent if available
        self.magnet = magnet

        # Holds status info so that we don't need to keep getting it from lt
        self.status = self.handle.status()

        try:
            self.torrent_info = self.handle.get_torrent_info()
        except RuntimeError:
            self.torrent_info = None

        self.has_metadata = self.handle.has_metadata()
        self.status_funcs = None

        # Default total_uploaded to 0, this may be changed by the state
        self.total_uploaded = 0

        # Set the default options
        self.options = TorrentOptions()
        self.options.update(options)

        # We need to keep track if the torrent is finished in the state to prevent
        # some weird things on state load.
        self.is_finished = False

        # Load values from state if we have it
        if state:
            # This is for saving the total uploaded between sessions
            self.total_uploaded = state.total_uploaded
            # Set the trackers
            self.set_trackers(state.trackers)
            # Set the filename
            self.filename = state.filename
            self.is_finished = state.is_finished
        else:
            # Tracker list
            self.trackers = []
            # Create a list of trackers
            for tracker in self.handle.trackers():
                self.trackers.append(tracker)

        # Various torrent options
        self.handle.resolve_countries(True)

        self.set_options(self.options)

        # Status message holds error info about the torrent
        self.statusmsg = "OK"

        # The torrents state
        # This is only one out of 4 calls to update_state for each torrent on startup.
        # This call doesn't seem to be necessary, it can probably be removed
        #self.update_state()
        self.state = None

        self.tracker_status = ""

        # This gets updated when get_tracker_host is called
        self.tracker_host = None

        if state:
            self.time_added = state.time_added
        else:
            self.time_added = time.time()

        # Keep track of the owner
        if state:
            self.owner = state.owner
        else:
            self.owner = owner

        # Keep track of last seen complete
        if state:
            self._last_seen_complete = state.last_seen_complete or 0.0
        else:
            self._last_seen_complete = 0.0

        # Keep track if we're forcing a recheck of the torrent so that we can
        # re-pause it after its done if necessary
        self.forcing_recheck = False
        self.forcing_recheck_paused = False

        self.update_status(self.handle.status())
        self._create_status_funcs()

        if log.isEnabledFor(logging.DEBUG):
            log.debug("Torrent object created.")

    def on_metadata_received(self):
        self.has_metadata = True
        self.torrent_info = self.handle.get_torrent_info()
        if self.options["prioritize_first_last_pieces"]:
            self.set_prioritize_first_last(True)
        self.write_torrentfile()

    ## Options methods ##
    def set_options(self, options):
        OPTIONS_FUNCS = {
            # Functions used for setting options
            "auto_managed": self.set_auto_managed,
            "download_location": self.set_save_path,
            "file_priorities": self.set_file_priorities,
            "max_connections": self.handle.set_max_connections,
            "max_download_speed": self.set_max_download_speed,
            "max_upload_slots": self.handle.set_max_uploads,
            "max_upload_speed": self.set_max_upload_speed,
            "prioritize_first_last_pieces": self.set_prioritize_first_last,
            "sequential_download": self.set_sequential_download
        }

        # set_prioritize_first_last is called by set_file_priorities,
        # so remove if file_priorities is set in options.
        if "file_priorities" in options:
            del OPTIONS_FUNCS["prioritize_first_last_pieces"]

        for (key, value) in options.items():
            if OPTIONS_FUNCS.has_key(key):
                OPTIONS_FUNCS[key](value)
        self.options.update(options)

    def get_options(self):
        return self.options

    def set_owner(self, account):
        self.owner = account

    def set_max_connections(self, max_connections):
        self.options["max_connections"] = int(max_connections)
        self.handle.set_max_connections(max_connections)

    def set_max_upload_slots(self, max_slots):
        self.options["max_upload_slots"] = int(max_slots)
        self.handle.set_max_uploads(max_slots)

    def set_max_upload_speed(self, m_up_speed):
        self.options["max_upload_speed"] = m_up_speed
        if m_up_speed < 0:
            v = -1
        else:
            v = int(m_up_speed * 1024)
        self.handle.set_upload_limit(v)

    def set_max_download_speed(self, m_down_speed):
        self.options["max_download_speed"] = m_down_speed
        if m_down_speed < 0:
            v = -1
        else:
            v = int(m_down_speed * 1024)
        self.handle.set_download_limit(v)

    def set_prioritize_first_last(self, prioritize):
        self.options["prioritize_first_last_pieces"] = prioritize
        if not prioritize:
            # If we are turning off this option, call set_file_priorities to
            # reset all the piece priorities
            self.set_file_priorities(self.options["file_priorities"])
            return
        if not self.has_metadata:
            return
        if self.options["compact_allocation"]:
            log.debug("Setting first/last priority with compact "
                      "allocation does not work!")
            return
        # A list of priorities for each piece in the torrent
        priorities = self.handle.piece_priorities()
        prioritized_pieces = []
        ti = self.torrent_info
        for i in range(ti.num_files()):
            f = ti.file_at(i)
            two_percent_bytes = int(0.02 * f.size)
            # Get the pieces for the byte offsets
            first_start = ti.map_file(i, 0, 0).piece
            first_end = ti.map_file(i, two_percent_bytes, 0).piece
            last_start = ti.map_file(i, f.size - two_percent_bytes, 0).piece
            last_end = ti.map_file(i, max(f.size - 1, 0), 0).piece

            first_end += 1
            last_end += 1
            prioritized_pieces.append((first_start, first_end))
            prioritized_pieces.append((last_start, last_end))

            # Creating two lists with priorites for the first/last pieces
            # of this file, and insert the priorities into the list
            first_list = [7] * (first_end - first_start)
            last_list = [7] * (last_end - last_start)
            priorities[first_start:first_end] = first_list
            priorities[last_start:last_end] = last_list
        # Setting the priorites for all the pieces of this torrent
        self.handle.prioritize_pieces(priorities)
        return prioritized_pieces, priorities

    def set_sequential_download(self, set_sequencial):
        self.options["sequential_download"] = set_sequencial
        self.handle.set_sequential_download(set_sequencial)

    def set_auto_managed(self, auto_managed):
        self.options["auto_managed"] = auto_managed
        if not (self.handle.is_paused() and not self.handle.is_auto_managed()):
            self.handle.auto_managed(auto_managed)
            self.update_state()

    def set_stop_ratio(self, stop_ratio):
        self.options["stop_ratio"] = stop_ratio

    def set_stop_at_ratio(self, stop_at_ratio):
        self.options["stop_at_ratio"] = stop_at_ratio

    def set_remove_at_ratio(self, remove_at_ratio):
        self.options["remove_at_ratio"] = remove_at_ratio

    def set_move_completed(self, move_completed):
        self.options["move_completed"] = move_completed

    def set_move_completed_path(self, move_completed_path):
        self.options["move_completed_path"] = move_completed_path

    def set_file_priorities(self, file_priorities):
        if not self.has_metadata:
            return
        if len(file_priorities) != self.torrent_info.num_files():
            log.debug("file_priorities len != num_files")
            self.options["file_priorities"] = self.handle.file_priorities()
            return

        if self.options["compact_allocation"]:
            log.debug("setting file priority with compact allocation does not work!")
            self.options["file_priorities"] = self.handle.file_priorities()
            return

        if log.isEnabledFor(logging.DEBUG):
            log.debug("setting %s's file priorities: %s", self.torrent_id, file_priorities)

        self.handle.prioritize_files(file_priorities)

        if 0 in self.options["file_priorities"]:
            # We have previously marked a file 'Do Not Download'
            # Check to see if we have changed any 0's to >0 and change state accordingly
            for index, priority in enumerate(self.options["file_priorities"]):
                if priority == 0 and file_priorities[index] > 0:
                    # We have a changed 'Do Not Download' to a download priority
                    self.is_finished = False
                    self.update_state()
                    break

        # In case values in file_priorities were faulty (old state?)
        # we make sure the stored options are in sync
        self.options["file_priorities"] = self.handle.file_priorities()

        # Set the first/last priorities if needed
        if self.options["prioritize_first_last_pieces"]:
            self.set_prioritize_first_last(self.options["prioritize_first_last_pieces"])

    def set_trackers(self, trackers):
        """Sets trackers"""
        if trackers == None:
            trackers = []
            for value in self.handle.trackers():
                tracker = {}
                tracker["url"] = value.url
                tracker["tier"] = value.tier
                trackers.append(tracker)
            self.trackers = trackers
            self.tracker_host = None
            return

        if log.isEnabledFor(logging.DEBUG):
            log.debug("Setting trackers for %s: %s", self.torrent_id, trackers)

        tracker_list = []

        for tracker in trackers:
            new_entry = lt.announce_entry(str(tracker["url"]))
            new_entry.tier = tracker["tier"]
            tracker_list.append(new_entry)
        self.handle.replace_trackers(tracker_list)

        # Print out the trackers
        if log.isEnabledFor(logging.DEBUG):
            for t in self.handle.trackers():
                log.debug("tier: %s tracker: %s", t["tier"], t["url"])
        # Set the tracker list in the torrent object
        self.trackers = trackers
        if len(trackers) > 0:
            # Force a re-announce if there is at least 1 tracker
            self.force_reannounce()

        self.tracker_host = None

    ### End Options methods ###

    def set_save_path(self, save_path):
        self.options["download_location"] = save_path

    def set_tracker_status(self, status):
        """Sets the tracker status"""
        self.tracker_status = self.get_tracker_host() + ": " + status

    def update_state(self):
        """Updates the state based on what libtorrent's state for the torrent is"""
        # Set the initial state based on the lt state
        LTSTATE = deluge.common.LT_TORRENT_STATE
        status = self.handle.status()
        ltstate = int(status.state)

        # Set self.state to the ltstate right away just incase we don't hit some
        # of the logic below
        if ltstate in LTSTATE:
            self.state = LTSTATE[ltstate]
        else:
            self.state = str(ltstate)

        session_is_paused = component.get("Core").session.is_paused()
        is_auto_managed = self.handle.is_auto_managed()
        handle_is_paused = self.handle.is_paused()

        if log.isEnabledFor(logging.DEBUG):
            log.debug("set_state_based_on_ltstate: %s", deluge.common.LT_TORRENT_STATE[ltstate])
            log.debug("session.is_paused: %s", session_is_paused)

        # First we check for an error from libtorrent, and set the state to that
        # if any occurred.
        if len(status.error) > 0:
            # This is an error'd torrent
            self.state = "Error"
            self.set_status_message(status.error)
            if handle_is_paused:
                self.handle.auto_managed(False)
            return

        if ltstate == LTSTATE["Queued"] or ltstate == LTSTATE["Checking"]:
            if handle_is_paused:
                self.state = "Paused"
            else:
                self.state = "Checking"
            return
        elif ltstate == LTSTATE["Downloading"] or ltstate == LTSTATE["Downloading Metadata"]:
            self.state = "Downloading"
        elif ltstate == LTSTATE["Finished"] or ltstate == LTSTATE["Seeding"]:
            self.state = "Seeding"
        elif ltstate == LTSTATE["Allocating"]:
            self.state = "Allocating"

        if not session_is_paused and handle_is_paused and is_auto_managed:
            self.state = "Queued"
        elif session_is_paused or (handle_is_paused and not is_auto_managed):
            self.state = "Paused"

    def set_state(self, state):
        """Accepts state strings, ie, "Paused", "Seeding", etc."""
        if state not in TORRENT_STATE:
            log.debug("Trying to set an invalid state %s", state)
            return

        self.state = state
        return

    def set_status_message(self, message):
        self.statusmsg = message

    def get_eta(self):
        """Returns the ETA in seconds for this torrent"""
        status = self.status
        if self.is_finished and self.options["stop_at_ratio"]:
            # We're a seed, so calculate the time to the 'stop_share_ratio'
            if not status.upload_payload_rate:
                return 0
            stop_ratio = self.options["stop_ratio"]
            return ((status.all_time_download * stop_ratio) - status.all_time_upload) / status.upload_payload_rate

        left = status.total_wanted - status.total_wanted_done

        if left <= 0 or status.download_payload_rate == 0:
            return 0

        try:
            eta = left / status.download_payload_rate
        except ZeroDivisionError:
            eta = 0

        return eta

    def get_ratio(self):
        """Returns the ratio for this torrent"""
        if self.status.total_done > 0:
            # We use 'total_done' if the downloaded value is 0
            downloaded = self.status.total_done
        else:
            # Return -1.0 to signify infinity
            return -1.0

        return float(self.status.all_time_upload) / float(downloaded)

    def get_files(self):
        """Returns a list of files this torrent contains"""
        if not self.has_metadata:
            return []
        ret = []
        files = self.torrent_info.files()
        for index, file in enumerate(files):
            ret.append({
                'index': index,
                'path': file.path.decode("utf8").replace('\\', '/'),
                'size': file.size,
                'offset': file.offset
            })
        return ret

    def get_peers(self):
        """Returns a list of peers and various information about them"""
        ret = []
        peers = self.handle.get_peer_info()

        for peer in peers:
            # We do not want to report peers that are half-connected
            if peer.flags & peer.connecting or peer.flags & peer.handshake:
                continue

            client = decode_string(str(peer.client))
            # Make country a proper string
            country = str()
            for c in peer.country:
                if not c.isalpha():
                    country += " "
                else:
                    country += c

            ret.append({
                "client": client,
                "country": country,
                "down_speed": peer.payload_down_speed,
                "ip": "%s:%s" % (peer.ip[0], peer.ip[1]),
                "progress": peer.progress,
                "seed": peer.flags & peer.seed,
                "up_speed": peer.payload_up_speed,
            })

        return ret

    def get_queue_position(self):
        """Returns the torrents queue position"""
        return self.handle.queue_position()

    def get_file_progress(self):
        """Returns the file progress as a list of floats.. 0.0 -> 1.0"""
        if not self.has_metadata:
            return 0.0

        file_progress = self.handle.file_progress()
        ret = []
        for i,f in enumerate(self.get_files()):
            try:
                ret.append(float(file_progress[i]) / float(f["size"]))
            except ZeroDivisionError:
                ret.append(0.0)

        return ret

    def get_tracker_host(self):
        """Returns just the hostname of the currently connected tracker
        if no tracker is connected, it uses the 1st tracker."""
        if self.tracker_host:
            return self.tracker_host

        tracker = self.status.current_tracker
        if not tracker and self.trackers:
            tracker = self.trackers[0]["url"]

        if tracker:
            url = urlparse(tracker.replace("udp://", "http://"))
            if hasattr(url, "hostname"):
                host = (url.hostname or 'DHT')
                # Check if hostname is an IP address and just return it if that's the case
                import socket
                try:
                    socket.inet_aton(host)
                except socket.error:
                    pass
                else:
                    # This is an IP address because an exception wasn't raised
                    return url.hostname

                parts = host.split(".")
                if len(parts) > 2:
                    if parts[-2] in ("co", "com", "net", "org") or parts[-1] in ("uk"):
                        host = ".".join(parts[-3:])
                    else:
                        host = ".".join(parts[-2:])
                self.tracker_host = host
                return host
        return ""

    def get_last_seen_complete(self):
        """
        Returns the time a torrent was last seen complete, ie, with all pieces
        available.
        """
        if lt.version_minor > 15:
            return self.status.last_seen_complete
        self.calculate_last_seen_complete()
        return self._last_seen_complete

    def get_status(self, keys, diff=False, update=False):
        """
        Returns the status of the torrent based on the keys provided

        :param keys: the keys to get the status on
        :type keys: list of str
        :param diff: if True, will return a diff of the changes since the last
        call to get_status based on the session_id
        :type diff: bool
        :param update: if True, the status will be updated from libtorrent
        if False, the cached values will be returned
        :type update: bool

        :returns: a dictionary of the status keys and their values
        :rtype: dict

        """
        if update:
            self.update_status(self.handle.status())

        if not keys:
            keys = self.status_funcs.keys()

        status_dict = {}

        for key in keys:
            status_dict[key] = self.status_funcs[key]()

        if diff:
            session_id = self.rpcserver.get_session_id()
            if session_id in self.prev_status:
                # We have a previous status dict, so lets make a diff
                status_diff = {}
                for key, value in status_dict.items():
                    if key in self.prev_status[session_id]:
                        if value != self.prev_status[session_id][key]:
                            status_diff[key] = value
                    else:
                        status_diff[key] = value

                self.prev_status[session_id] = status_dict
                return status_diff

            self.prev_status[session_id] = status_dict
            return status_dict

        return status_dict

    def update_status(self, status):
        """
        Updates the cached status.

        :param status: a libtorrent status
        :type status: libtorrent.torrent_status

        """
        self.status = status

    def _create_status_funcs(self):
        #if you add a key here->add it to core.py STATUS_KEYS too.
        self.status_funcs = {
            "active_time":            lambda: self.status.active_time,
            "all_time_download":      lambda: self.status.all_time_download,
            "compact":                lambda: self.options["compact_allocation"],
            "distributed_copies":     lambda: 0.0 if self.status.distributed_copies < 0 else \
                self.status.distributed_copies, # Adjust status.distributed_copies to return a non-negative value
            "download_payload_rate":  lambda: self.status.download_payload_rate,
            "file_priorities":        lambda: self.options["file_priorities"],
            "hash":                   lambda: self.torrent_id,
            "is_auto_managed":        lambda: self.options["auto_managed"],
            "is_finished":            lambda: self.is_finished,
            "max_connections":        lambda: self.options["max_connections"],
            "max_download_speed":     lambda: self.options["max_download_speed"],
            "max_upload_slots":       lambda: self.options["max_upload_slots"],
            "max_upload_speed":       lambda: self.options["max_upload_speed"],
            "message":                lambda: self.statusmsg,
            "move_on_completed_path": lambda: self.options["move_completed_path"],
            "move_on_completed":      lambda: self.options["move_completed"],
            "move_completed_path":    lambda: self.options["move_completed_path"],
            "move_completed":         lambda: self.options["move_completed"],
            "next_announce":          lambda: self.status.next_announce.seconds,
            "num_peers":              lambda: self.status.num_peers - self.status.num_seeds,
            "num_seeds":              lambda: self.status.num_seeds,
            "owner":                  lambda: self.owner,
            "paused":                 lambda: self.status.paused,
            "prioritize_first_last":  lambda: self.options["prioritize_first_last_pieces"],
            "sequential_download":    lambda: self.options["sequential_download"],
            "progress":               lambda: self.status.progress * 100,
            "shared":                 lambda: self.options["shared"],
            "remove_at_ratio":        lambda: self.options["remove_at_ratio"],
            "save_path":              lambda: self.options["download_location"],
            "seeding_time":           lambda: self.status.seeding_time,
            "seeds_peers_ratio":      lambda: -1.0 if self.status.num_incomplete == 0 else \
                self.status.num_complete / float(self.status.num_incomplete), # Use -1.0 to signify infinity
            "seed_rank":              lambda: self.status.seed_rank,
            "state":                  lambda: self.state,
            "stop_at_ratio":          lambda: self.options["stop_at_ratio"],
            "stop_ratio":             lambda: self.options["stop_ratio"],
            "time_added":             lambda: self.time_added,
            "total_done":             lambda: self.status.total_done,
            "total_payload_download": lambda: self.status.total_payload_download,
            "total_payload_upload":   lambda: self.status.total_payload_upload,
            "total_peers":            lambda: self.status.num_incomplete,
            "total_seeds":            lambda: self.status.num_complete,
            "total_uploaded":         lambda: self.status.all_time_upload,
            "total_wanted":           lambda: self.status.total_wanted,
            "tracker":                lambda: self.status.current_tracker,
            "trackers":               lambda: self.trackers,
            "tracker_status":         lambda: self.tracker_status,
            "upload_payload_rate":    lambda: self.status.upload_payload_rate,
            "comment":                lambda: decode_string(self.torrent_info.comment()) if self.has_metadata else u"",
            "num_files":              lambda: self.torrent_info.num_files() if self.has_metadata else 0,
            "num_pieces":             lambda: self.torrent_info.num_pieces() if self.has_metadata else 0,
            "piece_length":           lambda: self.torrent_info.piece_length() if self.has_metadata else 0,
            "private":                lambda: self.torrent_info.priv() if self.has_metadata else False,
            "total_size":             lambda: self.torrent_info.total_size() if self.has_metadata else 0,
            "eta":                    self.get_eta,
            "file_progress":          self.get_file_progress, # Adjust progress to be 0-100 value
            "files":                  self.get_files,
            "is_seed":                self.handle.is_seed,
            "peers":                  self.get_peers,
            "queue":                  self.handle.queue_position,
            "ratio":                  self.get_ratio,
            "tracker_host":           self.get_tracker_host,
            "last_seen_complete":     self.get_last_seen_complete,
            "name":                   self.get_name,
            "pieces":                 self._get_pieces_info,
            }

    def get_name(self):
        if self.has_metadata:
            name = self.torrent_info.file_at(0).path.replace("\\", "/", 1).split("/", 1)[0]
            if not name:
                name = self.torrent_info.name()
            return decode_string(name)
        elif self.magnet:
            try:
                keys = dict([k.split('=') for k in self.magnet.split('?')[-1].split('&')])
                name = keys.get('dn')
                if not name:
                    return self.torrent_id
                name = unquote(name).replace('+', ' ')
                return decode_string(name)
            except:
                pass
        return self.torrent_id

    def pause(self):
        """Pause this torrent"""
        # Turn off auto-management so the torrent will not be unpaused by lt queueing
        self.handle.auto_managed(False)
        if self.handle.is_paused():
            # This torrent was probably paused due to being auto managed by lt
            # Since we turned auto_managed off, we should update the state which should
            # show it as 'Paused'.  We need to emit a torrent_paused signal because
            # the torrent_paused alert from libtorrent will not be generated.
            self.update_state()
            component.get("EventManager").emit(TorrentStateChangedEvent(self.torrent_id, "Paused"))
        else:
            try:
                self.handle.pause()
            except Exception, e:
                log.debug("Unable to pause torrent: %s", e)
                return False

        return True

    def resume(self):
        """Resumes this torrent"""

        if self.handle.is_paused() and self.handle.is_auto_managed():
            log.debug("Torrent is being auto-managed, cannot resume!")
            return
        else:
            # Reset the status message just in case of resuming an Error'd torrent
            self.set_status_message("OK")

            if self.handle.is_finished():
                # If the torrent has already reached it's 'stop_seed_ratio' then do not do anything
                if self.options["stop_at_ratio"]:
                    if self.get_ratio() >= self.options["stop_ratio"]:
                        #XXX: This should just be returned in the RPC Response, no event
                        #self.signals.emit_event("torrent_resume_at_stop_ratio")
                        return

            if self.options["auto_managed"]:
                # This torrent is to be auto-managed by lt queueing
                self.handle.auto_managed(True)

            try:
                self.handle.resume()
            except:
                pass

            return True

    def connect_peer(self, ip, port):
        """adds manual peer"""
        try:
            self.handle.connect_peer((ip, int(port)), 0)
        except Exception, e:
            log.debug("Unable to connect to peer: %s", e)
            return False
        return True

    def move_storage(self, dest):
        """Move a torrent's storage location"""
        try:
            dest = unicode(dest, "utf-8")
        except TypeError:
            # String is already unicode
            pass

        if not os.path.exists(dest):
            try:
                # Try to make the destination path if it doesn't exist
                os.makedirs(dest)
            except IOError, e:
                log.exception(e)
                log.error("Could not move storage for torrent %s since %s does "
                          "not exist and could not create the directory.",
                          self.torrent_id, dest)
                return False

        dest_bytes = dest.encode('utf-8')
        try:
            # libtorrent needs unicode object if wstrings are enabled, utf8 bytestring otherwise
            try:
                self.handle.move_storage(dest)
            except TypeError:
                self.handle.move_storage(dest_bytes)
        except Exception, e:
            log.error("Error calling libtorrent move_storage: %s" % e)
            return False

        return True

    def save_resume_data(self):
        """Signals libtorrent to build resume data for this torrent, it gets
        returned in a libtorrent alert"""
        self.handle.save_resume_data()

    def write_torrentfile(self):
        """Writes the torrent file"""
        path = "%s/%s.torrent" % (
            os.path.join(get_config_dir(), "state"),
            self.torrent_id)
        log.debug("Writing torrent file: %s", path)
        try:
            # Regenerate the file priorities
            self.set_file_priorities([])
            md = lt.bdecode(self.torrent_info.metadata())
            torrent_file = {}
            torrent_file["info"] = md
            open(path, "wb").write(lt.bencode(torrent_file))
        except Exception, e:
            log.warning("Unable to save torrent file: %s", e)

    def delete_torrentfile(self):
        """Deletes the .torrent file in the state"""
        path = "%s/%s.torrent" % (
            os.path.join(get_config_dir(), "state"),
            self.torrent_id)
        log.debug("Deleting torrent file: %s", path)
        try:
            os.remove(path)
        except Exception, e:
            log.warning("Unable to delete the torrent file: %s", e)

    def force_reannounce(self):
        """Force a tracker reannounce"""
        try:
            self.handle.force_reannounce()
        except Exception, e:
            log.debug("Unable to force reannounce: %s", e)
            return False

        return True

    def scrape_tracker(self):
        """Scrape the tracker"""
        try:
            self.handle.scrape_tracker()
        except Exception, e:
            log.debug("Unable to scrape tracker: %s", e)
            return False

        return True

    def force_recheck(self):
        """Forces a recheck of the torrents pieces"""
        paused = self.handle.is_paused()
        try:
            self.handle.force_recheck()
            self.handle.resume()
        except Exception, e:
            log.debug("Unable to force recheck: %s", e)
            return False
        self.forcing_recheck = True
        self.forcing_recheck_paused = paused
        return True

    def rename_files(self, filenames):
        """Renames files in the torrent. 'filenames' should be a list of
        (index, filename) pairs."""
        for index, filename in filenames:
            # Make sure filename is a unicode object
            try:
                filename = unicode(filename, "utf-8")
            except TypeError:
                pass
            filename = sanitize_filepath(filename)
            # libtorrent needs unicode object if wstrings are enabled, utf8 bytestring otherwise
            try:
                self.handle.rename_file(index, filename)
            except TypeError:
                self.handle.rename_file(index, filename.encode("utf-8"))

    def rename_folder(self, folder, new_folder):
        """
        Renames a folder within a torrent.  This basically does a file rename
        on all of the folders children.

        :returns: A deferred which fires when the rename is complete
        :rtype: twisted.internet.defer.Deferred
        """
        log.debug("attempting to rename folder: %s to %s", folder, new_folder)
        if len(new_folder) < 1:
            log.error("Attempting to rename a folder with an invalid folder name: %s", new_folder)
            return

        new_folder = sanitize_filepath(new_folder, folder=True)

        def on_file_rename_complete(result, wait_dict, index):
            wait_dict.pop(index, None)

        wait_on_folder = {}
        self.waiting_on_folder_rename.append(wait_on_folder)
        for f in self.get_files():
            if f["path"].startswith(folder):
                # Keep track of filerenames we're waiting on
                wait_on_folder[f["index"]] = Deferred().addBoth(on_file_rename_complete, wait_on_folder, f["index"])
                new_path = f["path"].replace(folder, new_folder, 1)
                try:
                    self.handle.rename_file(f["index"], new_path)
                except TypeError:
                    self.handle.rename_file(f["index"], new_path.encode("utf-8"))

        def on_folder_rename_complete(result, torrent, folder, new_folder):
            component.get("EventManager").emit(TorrentFolderRenamedEvent(torrent.torrent_id, folder, new_folder))
            # Empty folders are removed after libtorrent folder renames
            self.remove_empty_folders(folder)
            torrent.waiting_on_folder_rename = filter(None, torrent.waiting_on_folder_rename)
            component.get("TorrentManager").save_resume_data((self.torrent_id,))

        d = DeferredList(wait_on_folder.values())
        d.addBoth(on_folder_rename_complete, self, folder, new_folder)
        return d

    def remove_empty_folders(self, folder):
        """
        Recursively removes folders but only if they are empty.
        Cleans up after libtorrent folder renames.

        """
        info = self.get_status(['save_path'])
        # Regex removes leading slashes that causes join function to ignore save_path
        folder_full_path = os.path.join(info['save_path'], re.sub("^/*", "", folder))
        folder_full_path = os.path.normpath(folder_full_path)

        try:
            if not os.listdir(folder_full_path):
                os.removedirs(folder_full_path)
                log.debug("Removed Empty Folder %s", folder_full_path)
            else:
                for root, dirs, files in os.walk(folder_full_path, topdown=False):
                    for name in dirs:
                        try:
                            os.removedirs(os.path.join(root, name))
                            log.debug("Removed Empty Folder %s", os.path.join(root, name))
                        except OSError as (errno, strerror):
                            from errno import ENOTEMPTY
                            if errno == ENOTEMPTY:
                                # Error raised if folder is not empty
                                log.debug("%s", strerror)

        except OSError as (errno, strerror):
            log.debug("Cannot Remove Folder: %s (ErrNo %s)", strerror, errno)

    def _cleanup_prev_status(self):
        """
        This method gets called to check the validity of the keys in the prev_status
        dict.  If the key is no longer valid, the dict will be deleted.

        """
        for key in self.prev_status.keys():
            if not self.rpcserver.is_session_valid(key):
                del self.prev_status[key]

    def calculate_last_seen_complete(self):
        if self._last_seen_complete+60 > time.time():
            # Simple caching. Only calculate every 1 min at minimum
            return self._last_seen_complete

        availability = self.handle.piece_availability()
        if filter(lambda x: x<1, availability):
            # Torrent does not have all the pieces
            return
        log.trace("Torrent %s has all the pieces. Setting last seen complete.",
                  self.torrent_id)
        self._last_seen_complete = time.time()

    def _get_pieces_info(self):
        if not self.has_metadata:
            return None

        pieces = {}
        # First get the pieces availability.
        availability = self.handle.piece_availability()
        # Pieces from connected peers
        for peer_info in self.handle.get_peer_info():
            if peer_info.downloading_piece_index < 0:
                # No piece index, then we're not downloading anything from
                # this peer
                continue
            pieces[peer_info.downloading_piece_index] = 2

        # Now, the rest of the pieces
        for idx, piece in enumerate(self.status.pieces):
            if idx in pieces:
                # Piece beeing downloaded, handled above
                continue
            elif piece:
                # Completed Piece
                pieces[idx] = 3
                continue
            elif availability[idx] > 0:
                # Piece not downloaded nor beeing downloaded but available
                pieces[idx] = 1
                continue
            # If we reached here, it means the piece is missing, ie, there's
            # no known peer with this piece, or this piece has not been asked
            # for so far.
            pieces[idx] = 0

        sorted_indexes = pieces.keys()
        sorted_indexes.sort()
        # Return only the piece states, no need for the piece index
        # Keep the order
        return [pieces[idx] for idx in sorted_indexes]
