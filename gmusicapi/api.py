#!/usr/bin/env python

# Copyright (c) 2012, Simon Weber
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the copyright holder nor the
#       names of the contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDERS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


"""`gmusicapi` enables interaction with Google Music.
This includes both web-client and Music Manager features.

This api is not supported nor endorsed by Google, and could break at any time.

**Respect Google in your use of the API.**
Use common sense: protocol compliance, reasonable load, etc.
"""

import time
import copy
import contextlib
from uuid import getnode as getmac
from socket import gethostname
from urllib2 import HTTPCookieProcessor, Request, build_opener

import requests

from gmusicapi.gmtools import tools
from gmusicapi.exceptions import (
    CallFailure, ParseException, ValidationException,
    AlreadyLoggedIn, NotLoggedIn
)
from gmusicapi.protocol import webclient, musicmanager, upload_pb2
from gmusicapi.utils import utils
from gmusicapi.utils.apilogging import UsesLog
from gmusicapi.utils.clientlogin import ClientLogin
from gmusicapi.utils.tokenauth import TokenAuth


class Api(UsesLog):
    def __init__(self, suppress_failure=False):
        """Initializes an Api.

        :param suppress_failure: when True, CallFailure will never be raised.
        """

        self.suppress_failure = suppress_failure

        self.session = PlaySession()

        self.uploader_id = None
        self.uploader_name = None

        self.init_logger()

    @contextlib.contextmanager
    def _unsuppress_failures(self):
        """An internal context manager to temporarily disable failure suppression.

        This should wrap any Api code which tries to catch CallFailure."""

        orig = self.suppress_failure
        self.suppress_failure = False
        try:
            yield
        finally:
            self.suppress_failure = orig

    #---
    #   Authentication:
    #---

    def is_authenticated(self):
        """Returns whether the api is logged in."""
        return self.session.logged_in

    def login(self, email, password, perform_upload_auth=True,
              uploader_id=None, uploader_name=None):
        """Authenticates the api with the given credentials.
        Returns True on success, False on failure.

        :param email: eg `test@gmail.com` or just `test`.
        :param password: plaintext password. It will not be stored and is sent over ssl.
        :param perform_upload_auth: if True, register/authenticate as an upload device
        :param uploader_id: unique id in mac address form, eg '01:23:45:67:89:AB'.
                            default is mac address incremented by 1, and should be used when
                            possible.
                            Upload behavior will be unexpected if a Music Manager uses the same id.
                            OSError will be raised if this is not provided and a mac could not be
                            determined (eg when running on a VPS). If provided, it's best to use
                            the same id on all future runs.
        :param uploader_name: human-readable non-unique id; default is "<hostname> (gmusicapi)"

        Users of two-factor authentication will need to set an application-specific password
        to log in.

        Uploads from this instance will send uploader_id and uploader_name.
        """

        self.session.login(email, password)
        if not self.is_authenticated():
            self.log.info("failed to authenticate")
            return False

        self.log.info("authenticated")

        if perform_upload_auth:
            if uploader_id is None:
                mac = getmac()
                if (mac >> 40) % 2:
                    raise OSError('uploader_id not provided, and a valid MAC could not be found.')
                else:
                    mac += 1 % (1 << 48)  # to distinguish us from a Music Manager on this machine

                mac = hex(mac)[2:-1]
                mac = ':'.join([mac[x:x + 2] for x in range(0, 10, 2)])
                uploader_id = mac.upper()

            if uploader_name is None:
                uploader_name = gethostname() + " (gmusicapi)"

            try:
                #self._mm_pb_call("upload_auth")
                self._make_call(musicmanager.AuthenticateUploader,
                                uploader_id,
                                uploader_name)
                self.log.info("successful upload auth")
                self.uploader_id = uploader_id
                self.uploader_name = uploader_name

            except CallFailure:
                self.log.exception("could not authenticate for uploading")
                self.session.logout()
                return False

        return True

    def logout(self):
        """Forgets local authentication without affecting the server.
        Always returns True.
        """
        self.session.logout()
        self.uploader_id = None
        self.uploader_name = None

        self.log.info("logged out")

        return True

    #---
    #   Api features supported by the web client interface:
    #---
    def change_playlist_name(self, playlist_id, new_name):
        """Changes the name of a playlist. Returns the changed id.

        :param playlist_id: id of the playlist to rename.
        :param new_title: desired title.
        """

        self._make_call(webclient.ChangePlaylistName, playlist_id, new_name)

        return playlist_id  # the call actually doesn't return anything.

    @utils.accept_singleton(dict)
    @utils.empty_arg_shortcircuit()
    def change_song_metadata(self, songs):
        """Changes the metadata for songs given in `GM Metadata Format`_.
        Returns a list of the song ids changed.

        :param songs: a list of song dictionaries, or a single song dictionary.


        The server response is *not* to be trusted.
        Instead, reload the library; this will always reflect changes.

        These metadata keys are able to be changed:

        * rating: set to 0 (no thumb), 1 (down thumb), or 5 (up thumb)
        * name: use this instead of `title`
        * album
        * albumArtist
        * artist
        * composer
        * disc
        * genre
        * playCount
        * totalDiscs
        * totalTracks
        * track
        * year

        These keys cannot be changed:

        * comment
        * id
        * deleted
        * creationDate
        * albumArtUrl
        * type
        * beatsPerMinute
        * url

        These keys cannot be changed; their values are determined by another key's value:

        * title: set to `name`
        * titleNorm: set to lowercase of `name`
        * albumArtistNorm: set to lowercase of `albumArtist`
        * albumNorm: set to lowercase of `album`
        * artistNorm: set to lowercase of `artist`

        These keys cannot be changed, and may change unpredictably:

        * lastPlayed: likely some kind of last-accessed timestamp
        """

        res = self._make_call(webclient.ChangeSongMetadata, songs)

        return [s['id'] for s in res['songs']]

    def create_playlist(self, name):
        """Creates a new playlist. Returns the new playlist id.

        :param title: the title of the playlist to create.
        """

        return self._make_call(webclient.AddPlaylist, name)['id']

    def delete_playlist(self, playlist_id):
        """Deletes a playlist. Returns the deleted id.

        :param playlist_id: id of the playlist to delete.
        """

        res = self._make_call(webclient.DeletePlaylist, playlist_id)

        return res['deleteId']

    @utils.accept_singleton(basestring)
    @utils.empty_arg_shortcircuit()
    def delete_songs(self, song_ids):
        """Deletes songs from the entire library. Returns a list of deleted song ids.

        :param song_ids: a list of song ids, or a single song id.
        """

        res = self._make_call(webclient.DeleteSongs, song_ids)

        return res['deleteIds']

    def get_all_songs(self):
        """Returns a list of `song dictionaries`__.

        __ `GM Metadata Format`_
        """

        library = []

        lib_chunk = self._make_call(webclient.GetLibrarySongs)

        while 'continuationToken' in lib_chunk:
            library += lib_chunk['playlist']  # 'playlist' is misleading; this is the entire chunk

            lib_chunk = self._make_call(webclient.GetLibrarySongs, lib_chunk['continuationToken'])

        library += lib_chunk['playlist']

        return library

    def get_playlist_songs(self, playlist_id):
        """Returns a list of `song dictionaries`__, which include `playlistEntryId` keys
        for the given playlist.

        :param playlist_id: id of the playlist to load.

        __ `GM Metadata Format`_
        """

        res = self._make_call(webclient.GetPlaylistSongs, playlist_id)
        return res['playlist']

    def get_all_playlist_ids(self, auto=True, user=True, always_id_lists=False):
        """Returns a dictionary mapping playlist types to dictionaries of ``{"<playlist name>": "<playlist id>"}`` pairs.

        Available playlist types are:

        * "`auto`" - auto playlists
        * "`user`" - user-defined playlists (including instant mixes)

        :param auto: make an "`auto`" entry in the result.
        :param user: make a "`user`" entry in the result.
        :param always_id_lists: when False, map name -> id when there is a single playlist for that name. When True, always map to a list (which may only have a single id in it).

        Google Music allows for multiple playlists of the same name. Since this is uncommon, `always_id_lists` is False by default: names will map directly to ids when unique. However, this can create ambiguity if the api user doesn't have advance knowledge of the playlists. In this case, setting `always_id_lists` to True is recommended.

        Note that playlist names can be unicode strings.
        """

        playlists = {}

        if auto:
            playlists['auto'] = self._get_auto_playlists()
        if user:
            res = self._make_call(webclient.GetPlaylistSongs, 'all')
            playlists['user'] = self._playlist_list_to_dict(res['playlists'])

        #Break down singleton lists if desired.
        if not always_id_lists:
            for p_dict in playlists.itervalues():
                for name, id_list in p_dict.iteritems():
                    if len(id_list) == 1:
                        p_dict[name] = id_list[0]

        return playlists

    def _playlist_list_to_dict(self, pl_list):
        ret = {}

        for name, pid in ((p["title"], p["playlistId"]) for p in pl_list):
            if not name in ret:
                ret[name] = []
            ret[name].append(pid)

        return ret

    def _get_auto_playlists(self):
        """For auto playlists, returns a dictionary which maps autoplaylist name to id."""

        #Auto playlist ids are hardcoded in the wc javascript.
        #If Google releases Music internationally, this might be broken.
        #When testing, an incorrect name here will be caught.
        return {"Thumbs up": "auto-playlist-thumbs-up",
                "Last added": "auto-playlist-recent",
                "Free and purchased": "auto-playlist-promo"}

    def get_song_download_info(self, song_id):
        """Returns a tuple ``("<download url>", <download count>)``.

        GM allows 2 downloads per song.
        This call does not register a download - that is done when the download url is retrieved.

        :param song_id: a single song id.
        """

        #TODO the protocol expects a list of songs - could extend with accept_singleton
        info = self._make_call(webclient.GetDownloadInfo, [song_id])

        return (info["url"], info["downloadCounts"][song_id])

    def get_stream_url(self, song_id):
        """Returns a url that points to a streamable version of this song.

        While this call requires authentication, listening to the returned url does not.
        The url expires after about a minute.

        :param song_id: a single song id.

        *This is only intended for streaming*. The streamed audio does not contain metadata.
        Use :func:`get_song_download_info` to download complete files with metadata.
        """

        res = self._make_call(webclient.GetStreamUrl, song_id)

        return res['url']

    def copy_playlist(self, orig_id, copy_name):
        """Copies the contents of a playlist to a new playlist. Returns the id of the new playlist.

        :param orig_id: id of the playlist to be copied.
        :param copy_name: the name of the new copied playlist.

        Useful for making backups of playlists before modifications.
        """
        
        orig_tracks = self.get_playlist_songs(orig_id)
        
        new_id = self.create_playlist(copy_name)
        self.add_songs_to_playlist(new_id, [t["id"] for t in orig_tracks])

        return new_id

    def change_playlist(self, playlist_id, desired_playlist, safe=True):
        """Changes the order and contents of an existing playlist. Returns the id of the playlist when finished - which may not be the argument, in the case of a failure and recovery.
        
        :param playlist_id: the id of the playlist being modified.
        :param desired_playlist: the desired contents and order as a list of song dictionaries, like is returned from :func:`get_playlist_songs`.
        :param safe: if True, ensure playlists will not be lost if a problem occurs. This may slow down updates.

        The server only provides 3 basic (atomic) playlist mutations: addition, deletion, and reordering. This function will automagically use these to apply a list representation of the desired changes.

        However, this might involve multiple calls to the server, and if a call fails, the playlist will be left in an inconsistent state. The `safe` option makes a backup of the playlist before doing anything, so it can be rolled back if a problem occurs. This is enabled by default. Note that this might slow down updates of very large playlists.

        There will always be a warning logged if a problem occurs, even if `safe` is False.
        """
        
        #We'll be modifying the entries in the playlist, and need to copy it.
        #Copying ensures two things:
        # 1. the user won't see our changes
        # 2. changing a key for one entry won't change it for another - which would be the case
        #     if the user appended the same song twice, for example.
        desired_playlist = [copy.deepcopy(t) for t in desired_playlist]
        server_tracks = self.get_playlist_songs(playlist_id)

        if safe: #make the backup.
            #The backup is stored on the server as a new playlist with "_gmusicapi_backup" appended to the backed up name.
            #We can't just store the backup here, since when rolling back we'd be relying on this function - and it just failed.
            names_to_ids = self.get_all_playlist_ids(always_id_lists=True)['user']
            playlist_name = (ni_pair[0] 
                             for ni_pair in names_to_ids.iteritems()
                             if playlist_id in ni_pair[1]).next()

            backup_id = self.copy_playlist(playlist_id, playlist_name + "_gmusicapi_backup")


        #Ensure CallFailures do not get suppressed in our subcalls.
        #Did not unsuppress the above copy_playlist call, since we should fail 
        # out if we can't ensure the backup was made.
        with self._unsuppress_failures():
            try:
                #Counter, Counter, and set of id pairs to delete, add, and keep.
                to_del, to_add, to_keep = tools.find_playlist_changes(server_tracks, desired_playlist)

                ##Delete unwanted entries.
                to_del_eids = [pair[1] for pair in to_del.elements()]
                if to_del_eids: self._remove_entries_from_playlist(playlist_id, to_del_eids)

                ##Add new entries.
                to_add_sids = [pair[0] for pair in to_add.elements()]
                if to_add_sids:
                    new_pairs = self.add_songs_to_playlist(playlist_id, to_add_sids)

                    ##Update desired tracks with added tracks server-given eids.
                    #Map new sid -> [eids]
                    new_sid_to_eids = {}
                    for sid, eid in new_pairs:
                        if not sid in new_sid_to_eids:
                            new_sid_to_eids[sid] = []
                        new_sid_to_eids[sid].append(eid)


                    for d_t in desired_playlist:
                        if d_t["id"] in new_sid_to_eids:
                            #Found a matching sid.
                            match = d_t
                            sid = match["id"]
                            eid = match.get("playlistEntryId") 
                            pair = (sid, eid)

                            if pair in to_keep:
                                to_keep.remove(pair) #only keep one of the to_keep eids.
                            else:
                                match["playlistEntryId"] = new_sid_to_eids[sid].pop()
                                if len(new_sid_to_eids[sid]) == 0:
                                    del new_sid_to_eids[sid]


                ##Now, the right eids are in the playlist.
                ##Set the order of the tracks:

                #The web client has no way to dictate the order without block insertion,
                # but the api actually supports setting the order to a given list.
                #For whatever reason, though, it needs to be set backwards; might be
                # able to get around this by messing with afterEntry and beforeEntry parameters.
                sids, eids = zip(*tools.get_id_pairs(desired_playlist[::-1]))


                if sids:
                    self._make_call(webclient.ChangePlaylistOrder, playlist_id, sids, eids)

                ##Clean up the backup.
                if safe: self.delete_playlist(backup_id)

            except CallFailure:
                self.log.warning("a subcall of change_playlist failed - playlist %s is in an inconsistent state", playlist_id)

                if not safe: raise #there's nothing we can do
                else: #try to revert to the backup
                    self.log.warning("attempting to revert changes from playlist '%s_gmusicapi_backup'", playlist_name)

                    try:
                        self.delete_playlist(playlist_id)
                        self.change_playlist_name(backup_id, playlist_name)
                    except CallFailure:
                        self.log.error("failed to revert changes.")
                        raise
                    else:
                        self.log.warning("reverted changes safely; playlist id of '%s' is now '%s'", playlist_name, backup_id)
                        playlist_id = backup_id

            return playlist_id

    @utils.accept_singleton(basestring, 2)
    @utils.empty_arg_shortcircuit(position=2)
    def add_songs_to_playlist(self, playlist_id, song_ids):
        """Appends songs to a playlist.
        Returns a list of (song id, playlistEntryId) tuples that were added.

        :param playlist_id: id of the playlist to add to.
        :param song_ids: a list of song ids, or a single song id.
        """

        res = self._make_call(webclient.AddToPlaylist, playlist_id, song_ids)
        new_entries = res['songIds']

        return [(e['songId'], e['playlistEntryId']) for e in new_entries]


    @utils.accept_singleton(basestring, 2)
    @utils.empty_arg_shortcircuit(position=2)
    def remove_songs_from_playlist(self, playlist_id, sids_to_match):
        """Removes all copies of the given song ids from a playlist. Returns a list of removed (sid, eid) pairs.

        :param playlist_id: id of the playlist to remove songs from.
        :param sids_to_match: a list of songids to match, or a single song id.

        This does *not always* the inverse of a call to :func:`add_songs_to_playlist`, since multiple copies of the same song are removed. For more control in this case, get the playlist tracks with :func:`get_playlist_songs`, modify the list of tracks, then use :func:`change_playlist` to push changes to the server.
        """

        playlist_tracks = self.get_playlist_songs(playlist_id)
        sid_set = set(sids_to_match)

        matching_eids = [t["playlistEntryId"]
                         for t in playlist_tracks
                         if t["id"] in sid_set]

        if matching_eids:
            #Call returns "sid_eid" strings.
            sid_eids = self._remove_entries_from_playlist(playlist_id, 
                                                          matching_eids)
            return [s.split("_") for s in sid_eids]
        else:
            return []
    
    @utils.accept_singleton(basestring, 2)
    @utils.empty_arg_shortcircuit(position=2)
    def _remove_entries_from_playlist(self, playlist_id, entry_ids_to_remove):
        """Removes entries from a playlist. Returns a list of removed "sid_eid" strings.

        :param playlist_id: the playlist to be modified.
        :param entry_ids: a list of entry ids, or a single entry id.
        """

        #GM requires the song ids in the call as well; find them.
        playlist_tracks = self.get_playlist_songs(playlist_id)
        remove_eid_set = set(entry_ids_to_remove)
        
        e_s_id_pairs = [(t["id"], t["playlistEntryId"]) 
                        for t in playlist_tracks
                        if t["playlistEntryId"] in remove_eid_set]

        num_not_found = len(entry_ids_to_remove) - len(e_s_id_pairs)
        if num_not_found > 0:
            self.log.warning("when removing, %d entry ids could not be found in playlist id %s", num_not_found, playlist_id)

        #Unzip the pairs.
        sids, eids = zip(*e_s_id_pairs)

        res = self._make_call(webclient.DeleteSongs, sids, playlist_id, eids)

        return res['deleteIds']

    def search(self, query):
        """Queries the server for songs and albums.
        Generally, this isn't needed; just get all tracks and locally search over them.

        :param query: the search query.

        Search results are organized based on how they were found.
        Hits on an album title return information on that album. Here is an example album result::

            {'artistName': 'The Cat Empire',
             'imageUrl': '<url>',
             'albumArtist': 'The Cat Empire',
             'albumName': 'Cities: The Cat Empire Project'}

        Hits on song or artist name return the matching `song dictionary`__.

        The responses are returned in a dictionary, arranged by hit type::

              {'album_hits':[<album dictionary>, ...],
               'artist_hits':[<song dictionary>, ...],
               'song_hits':[<song dictionary>, ...]}

        The search ignores punctuation.

        __ `GM Metadata Format`_
        """

        res = self._make_call(webclient.Search, query)['results']

        return {"album_hits": res["albums"],
                "artist_hits": res["artists"],
                "song_hits": res["songs"]}

    @utils.accept_singleton(basestring)
    @utils.empty_arg_shortcircuit()
    def report_incorrect_match(self, song_ids):
        """Equivalent to the 'Fix Incorrect Match' button, this requests re-uploading of songs.
        Returns the song_ids given.

        :param song_ids: a list of songids to report, or a single song id.

        Note that if you uploaded the song through this api, it won't be reuploaded
        automatically - this currently only works for songs uploaded with the Music Manager.

        This should only be used on matched tracks with song['type'] == 6.
        """

        self._make_call(webclient.ReportBadSongMatch, song_ids)

        return song_ids

    @utils.accept_singleton(basestring)
    @utils.empty_arg_shortcircuit()
    def change_album_art(self, song_ids, image_filepath):
        """Change the album art of songs.

        :param song_ids: a list of song ids, or a single song id
        :param image_filepath: filepath of the art to use. jpg and png are known to work.

        Note that this always uploads the given art. If you already have the art uploaded and set
        for another song, you can just copy over the the 'albumArtUrl' key, then set the change
        with :func:`change_song_metadata`.
        """

        with open(image_filepath) as f:
            image = f.read()

        res = self._make_call(webclient.UploadImage, image)
        url = res['imageUrl']

        song_dicts = [{'id': id, 'albumArtUrl': url} for id in song_ids]

        return self.change_song_metadata(song_dicts)

    #---
    #   Api features supported by the Music Manager interface:
    #---

    # def get_quota(self):
    #     """Returns a tuple of (allowed number of tracks, total tracks, available tracks)."""
    #     quota = self._mm_pb_call("client_state").quota
    #     #protocol incorrect here...
    #     return (quota.maximumTracks, quota.totalTracks, quota.availableTracks)

    @utils.accept_singleton(basestring)
    @utils.empty_arg_shortcircuit(return_code='{}')
    def upload(self, filepaths):
        """Uploads the given filepaths.
        Return a 3-tuple (uploaded, matched, not_uploaded) of dictionaries:
            uploaded: {filepath: new server id}
            matched: {filepath: new server id}
            not_uploaded: {filepath: string reason (eg 'ALREADY_UPLOADED')}

        :param filepaths: a list of filepaths, or a single filepath.

        All Google-supported filetypes are supported; see http://goo.gl/iEwLI for more information.

        Unlike Google's Music Manager, this function will currently allow the same song to
        be uploaded more than once if its tags are changed. This is subject to change in the future.
        """
        if self.uploader_id is None or self.uploader_name is None:
            raise NotLoggedIn("Not authenticated as an upload device;"
                              " run Api.login(...perform_upload_auth=True...)"
                              " first.")

        #To return.
        uploaded = {}
        matched = {}
        not_uploaded = {}

        #Gather local information on the files.
        local_info = {}  # {clientid: (path, contents, Track)}
        for path in filepaths:
            try:
                with open(path, 'rb') as f:
                    contents = f.read()
                track = musicmanager.UploadMetadata.fill_track_info(path, contents)
            except (IOError, ValueError) as e:
                self.log.exception("problem gathering local info of '%s'" % path)
                not_uploaded[path] = str(e)
            else:
                local_info[track.client_id] = (path, contents, track)

        if not local_info:
            return uploaded, matched, not_uploaded

        #TODO allow metadata faking

        #Upload metadata; the server tells us what to do next.
        res = self._make_call(musicmanager.UploadMetadata,
                              [track for (path, contents, track) in local_info.values()],
                              self.uploader_id)

        #TODO checking for proper contents should be handled in verification
        md_res = res.metadata_response

        responses = [r for r in md_res.track_sample_response]
        sample_requests = [req for req in md_res.signed_challenge_info]

        #Send scan and match samples if requested.
        for sample_request in sample_requests:
            path, contents, track = local_info[sample_request.challenge_info.client_track_id]

            try:
                res = self._make_call(musicmanager.ProvideSample,
                                      contents, sample_request, track, self.uploader_id)
            except ValueError as e:
                self.log.warning("couldn't create scan and match sample for '%s': %s", path, str(e))
                not_uploaded[path] = str(e)
            else:
                responses.extend(res.sample_response.track_sample_response)

        #Read sample responses and prep upload requests.
        to_upload = {}  # {serverid: (path, contents, Track)}
        for res in responses:
            path, contents, track = local_info[res.client_track_id]

            if res.response_code == upload_pb2.TrackSampleResponse.MATCHED:
                matched[path] = res.server_track_id
            elif res.response_code == upload_pb2.TrackSampleResponse.UPLOAD_REQUESTED:
                to_upload[res.server_track_id] = (path, contents, track)
            else:
                #Get the symbolic name of the response code enum:
                enum_desc = upload_pb2._TRACKSAMPLERESPONSE.enum_types[0]
                res_name = enum_desc.values_by_number[res.response_code].name

                err_msg = "TrackSampleResponse error %s: %s" % (res.response_code, res_name)

                self.log.warning("upload of '%s' rejected: %s", path, err_msg)
                not_uploaded[path] = err_msg

        #Send upload requests.
        if to_upload:
            self._make_call(musicmanager.UpdateUploadState, 'start', self.uploader_id)

            for server_id, (path, contents, track) in to_upload.items():
                #It can take a few tries to get an session.
                should_retry = True
                attempts = 0

                while should_retry and attempts < 5:
                    session = self._make_call(musicmanager.GetUploadSession,
                                              self.uploader_id, len(uploaded),
                                              track, path, server_id)
                    attempts += 1

                    got_session, error_details = \
                        musicmanager.GetUploadSession.process_session(session)

                    if got_session:
                        self.log.info("got an upload session for '%s'", path)
                        break

                    should_retry, reason, error_code = error_details
                    self.log.debug("problem getting upload session: %s\ncode=%s retrying=%s",
                                   reason, error_code, should_retry)
                    time.sleep(5)  # wait before retrying
                else:
                    err_msg = "GetUploadSession error %s: %s" % (error_code, reason)

                    self.log.warning("giving up on upload session for '%s': %s", path, err_msg)
                    not_uploaded[path] = err_msg

                    continue  # to next upload

                #got a session, do the upload
                #this terribly inconsistent naming isn't my fault: Google--
                session = session['sessionStatus']
                external = session['externalFieldTransfers'][0]

                session_url = external['putInfo']['url']
                content_type = external['content_type']

                upload_response = self._make_call(musicmanager.UploadFile,
                                                  session_url, content_type, contents)

                success = upload_response.get('sessionStatus', {}).get('state')
                if success:
                    uploaded[path] = server_id
                else:
                    #think 404 == already uploaded. serverside check on clientid?
                    self.debug.log("could not finalize upload of '%s'. response: %s",
                                   path, upload_response)
                    not_uploaded[path] = 'could not finalize upload'

            self._make_call(musicmanager.UpdateUploadState, 'stopped', self.uploader_id)

        return uploaded, matched, not_uploaded

    def _make_call(self, protocol, *args, **kwargs):
        """Returns the response of a protocol.Call.
        Additional kw/args are passed to protocol.build_transaction."""

        call_name = protocol.__name__

        self.log.debug("%s(args=%s, kwargs=%s)",
                       call_name,
                       [utils.truncate(a) for a in args],
                       {k: utils.truncate(v) for (k, v) in kwargs.items()})

        request = protocol.build_request(*args, **kwargs)

        response = self.session.send(request, protocol.get_auth(), protocol.session_options)

        #TODO check return code

        try:
            msg = protocol.parse_response(response)
        except ParseException:
            self.log.exception("couldn't parse %s response: %r", call_name, response.content)
            if not self.suppress_failure:
                raise CallFailure("the server's response could not be understood."
                                  " The call may still have succeeded, but it's unlikely.",
                                  call_name)
            else:
                #TODO what happens now?
                msg = None

        self.log.debug(protocol.filter_response(msg))

        try:
            #order is important; validate only has a schema for a successful response
            protocol.check_success(msg)
            protocol.validate(msg)
        except CallFailure:
            if not self.suppress_failure:
                raise
            else:
                self.log.exception('the server responded that the call failed.'
                                   ' This is usually caused by invalid arguments.',
                                   call_name)
        except ValidationException:
            #TODO link to some protocol for reporting this
            self.log.exception(
                "please report the following unknown response format for %s: %r",
                call_name, msg
            )

        return msg


#---
#The session layer:
#---

class PlaySession(object):
    """
    A Google Play Music session.

    It allows for authentication and the making of authenticated
    requests through the MusicManager API (protocol buffers), Web client requests,
    and the Skyjam client API.
    """

    # The URL for authenticating against Google Play Music
    PLAY_URL = 'https://play.google.com/music/listen?u=0&hl=en'

    # Common User Agent used for web requests
    _user_agent = (
        "Mozilla/5.0 (X11; U; Linux i686; en-US; rv:1.8.1.6) "
        "Gecko/20061201 Firefox/2.0.0.6 (Ubuntu-feisty)"
    )

    def __init__(self):
        """
        Initializes a default unauthenticated session.
        """
        self.client = None
        self.web_cookies = None
        self.logged_in = False

    def _get_cookies(self):
        """
        Gets cookies needed for web and media streaming access.
        Returns True if the necessary cookies are found, False otherwise.
        """
        if self.logged_in:
            raise AlreadyLoggedIn

        handler = build_opener(HTTPCookieProcessor(self.web_cookies))
        req = Request(self.PLAY_URL, None, {})  # header
        resp_obj = handler.open(req)  # TODO is this necessary?

        return (
            self.get_web_cookie('sjsaid') is not None and
            self.get_web_cookie('xt') is not None
        )

    def get_web_cookie(self, name):
        """
        Finds the value of a cookie by name, returning None on failure.

        :param name: The name of the cookie to find.
        """
        if self.web_cookies is None:
            return None

        for cookie in self.web_cookies:
            if cookie.name == name:
                return cookie.value

        return None

    def login(self, email, password):
        """
        Attempts to create an authenticated session using the email and
        password provided.
        Return True if the login was successful, False otherwise.
        Raises AlreadyLoggedIn if the session is already authenticated.

        :param email: The email address of the account to log in.
        :param password: The password of the account to log in.
        """
        if self.logged_in:
            raise AlreadyLoggedIn

        self.client = ClientLogin(email, password, 'sj')
        tokenauth = TokenAuth('sj', self.PLAY_URL, 'jumper')

        if self.client.get_auth_token() is None:
            return False

        tokenauth.authenticate(self.client)
        self.web_cookies = tokenauth.get_cookies()

        self.logged_in = self._get_cookies()

        return self.logged_in

    def logout(self):
        """
        Resets the session to an unauthenticated default state.
        """
        self.__init__()

    def send(self, request, auth, session_options):
        """Send a request from a Call.

        :param request: filled requests.Request
        :param auth: result of Call.get_auth()
        :param session_options: dict of kwargs to pass to requests.Session.send
        """

        if any(auth) and not self.logged_in:
            raise NotLoggedIn

        send_xt, send_clientlogin, send_sso = auth

        if request.cookies is None:
            request.cookies = {}

        #Attach auth.
        if send_xt:
            request.params['u'] = 0
            request.params['xt'] = self.get_web_cookie('xt')

        if send_clientlogin:
            request.cookies['SID'] = self.client.get_sid_token()

        if send_sso:
            #dict <- CookieJar
            web_cookies = {c.name: c.value for c in self.web_cookies}
            request.cookies.update(web_cookies)

        prepped = request.prepare()
        s = requests.Session()

        res = s.send(prepped, **session_options)
        return res
