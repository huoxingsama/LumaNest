"""``ItemActions`` — the things a user does *to* an item.

Step 6c's second prep, and it exists for the same reason 6b did: the budget
said so. With rendering extracted, measuring the detail/series/season family
against ``PageContext`` still cost twelve-odd ``ctx.shell`` uses, and every
one of them was an action rather than a widget — toggle watched, favourite,
download, play, play the next episode.

So this is the action counterpart to :class:`~.tile_renderer.TileRenderer`.
Where that one turns items into pixels, this one turns a click into an
effect: it composes the raw capability (already on ``PlayerGateway``) with
the four things that make it a *user* action rather than an API call —

* run it off the loop thread, because every one of these is a round trip;
* apply it optimistically and roll back if the server refuses, because a
  tick that appears and silently reverts on the next reload is worse than
  one that never appeared;
* say something when it fails, because a swallowed error looks exactly like
  success;
* wake the render loop, because none of the above runs on it.

**Why the constructor looks like this.** ``services`` is a live provider,
read through and never snapshotted, exactly as ``TileRenderer.art`` is: the
browser swaps ``source`` and ``controller`` on a reconnect or a user switch,
and an action holding a stale controller would mutate the server the user
just left. The rest are callbacks for things only the shell can do — show a
dialog over the page, name the loading spinner, refresh the download badges.

That the list is this long is not an accident of the extraction; it is the
finding. A user action genuinely touches the data source, the player, the
dialog layer and the chrome. Bundling it here is what lets a *page* touch
none of them.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import random
from ..i18n import _
from ..utils import item_is_audio
log = logging.getLogger('mpvtk_browser.item_actions')

class ItemActions:
    """User actions on library items: play, mark, download."""

    def __init__(self, services, run, dialogs, on_launch, on_downloads_changed=None):
        self.services = services
        self.run = run
        self.dialogs = dialogs
        self._on_launch = on_launch
        self._on_downloads_changed = on_downloads_changed or (lambda: None)
        self._edit_ok = None
        self._record_ok = None
        self._pending_reads: dict = {}

    def _fire(self, fn):
        """A fire-and-forget mutation. Nothing waits on the result, and a
        failure must not escape onto a pool worker's traceback."""

        def task():
            try:
                fn(self.services.controller)
            except Exception:
                log.warning('client action failed', exc_info=True)
        self.run.submit(task)

    def _launch(self, work):
        """Start playback off the loop thread.

        ``work(ctl)`` runs on a pool worker. Starting playback is seconds of
        work — build a Media, ask for PlaybackInfo, load the file under the
        player's lock — and running it inline froze the UI from the click
        until playback began.

        Deliberately NOT epoch-gated: the user pressed Play, and navigating
        elsewhere while it starts is not a reason to cancel it.
        """
        ctl = self.services.controller
        if ctl is None:
            return

        def failed(_exc):
            self.services.set_status(_('Playback could not be started.'))
            self.services.invalidate()
        self.run.run(lambda: work(ctl), lambda _r: None, self.run.epoch, on_error=failed)

    def edit(self, fn, on_ok=None, on_error=None, error=None):
        """A mutating edit whose failure the user must see.

        The swallowing variant made an "Add to play queue" the server
        rejected look
        exactly like one that worked. ``on_error`` undoes whatever the view
        already showed optimistically — leaving a rejected change on screen is
        worse than never showing it.
        """
        msg = error or _('The change could not be applied.')

        def work():
            fn(self.services.controller)

        def done(_ok):
            if on_ok is not None:
                on_ok()

        def failed(_exc):
            if on_error is not None:
                on_error()
            self.services.set_status(msg)
        self.run.run(work, done, self.run.epoch, on_error=failed)

    def skip_to(self, playlist_item_id):
        """Jump the player to a queue entry."""
        ctl = self.services.controller
        if not playlist_item_id or ctl is None:
            return
        holding = getattr(ctl, 'playing_item_id', None)
        if holding is not None and holding() is None:
            self.services.set_status(_('Playback has stopped, so this queue is gone.'))
            return
        self._fire(lambda c: c.skip_to(playlist_item_id))

    def play(self, item, server, offset_ticks=None, srcid=None, aid=None, sid=None):
        """Yield/keep-browse and start a single ``item``. Episodes queue the
        rest of the series so autoplay-next chains them (like the Tk
        browser)."""
        self._on_launch(audio=item_is_audio(item), title=item.get('Name') or '')
        if self.services.controller is None:
            return
        if item.get('Type') == 'Episode' and item.get('SeriesId'):
            srv, iid, series = (server, item.get('Id'), item.get('SeriesId'))
            source = self.services.source

            def work(ctl):
                try:
                    q = source.get_series_queue(srv, series, start_item_id=iid)
                    ids = [e.get('Id') for e in q if e.get('Id')] or [iid]
                except Exception:
                    log.debug('series queue fetch failed', exc_info=True)
                    ids = [iid]
                ctl.play_list(ids, srv, 0, offset_ticks=offset_ticks, srcid=srcid, aid=aid, sid=sid)
            self._launch(work)
        else:
            self._launch(lambda ctl: ctl.play(item, server, offset_ticks=offset_ticks, srcid=srcid, aid=aid, sid=sid))

    def play_list(self, ids, server, start_index=0, audio=False, items=None, pause_stills=True):
        """Play a whole list from ``start_index`` (album/playlist/song).

        ``items`` (the DTOs behind ``ids``) supplies the resume offset for
        the entry actually being started, as the Tk browser does —
        without it, clicking a half-watched entry restarted it from zero.

        The chosen entry is re-located by id after dropping empty ones:
        filtering first and trusting the caller's index shifted the queue
        out from under the entry that was clicked.

        ``pause_stills`` says whether a photo at the head of the queue opens
        paused -- true for "show me this picture", false for "run the
        slideshow". See PlayerManager.play."""
        start_id = ids[start_index] if 0 <= start_index < len(ids) else None
        offset = None
        title = ''
        if items is not None and 0 <= start_index < len(items):
            offset = (items[start_index].get('UserData') or {}).get('PlaybackPositionTicks') or None
            title = items[start_index].get('Name') or ''
        ids = [i for i in ids if i]
        if not ids:
            return
        try:
            pos = ids.index(start_id)
        except ValueError:
            pos = 0
        self._on_launch(audio=audio, title=title)
        self._launch(lambda ctl: ctl.play_list(ids, server, pos, offset_ticks=offset, pause_stills=pause_stills))

    def play_shuffle(self, ids, server, audio=True):
        ids = [i for i in ids if i]
        random.shuffle(ids)
        self.play_list(ids, server, 0, audio=audio)

    def queue_items(self, ids, server):
        self.edit(lambda c: c.queue_items(server, [i for i in ids if i]), error=_('Those items could not be added to the queue.'))

    def queue_next_items(self, ids, server):
        """Queue after the current item rather than at the end of the queue."""
        self.edit(lambda c: c.queue_next_items(server, [i for i in ids if i]), error=_('Those items could not be added to the queue.'))

    def instant_mix(self, seed_id, server):
        ep = self.run.epoch
        source = self.services.source

        def work():
            return source.get_instant_mix(server, seed_id)

        def done(items):
            self.play_list([i.get('Id') for i in items], server, 0, audio=True)
        self.run.run(work, done, ep)

    def play_next_up(self, series_id, server):
        ep = self.run.epoch
        source = self.services.source

        def work():
            item = source.get_next_up(server, series_id)
            if item is None:
                first = source.get_series_queue(server, series_id, limit=1)
                item = first[0] if first else None
            return item

        def done(item):
            if item:
                offset = (item.get('UserData') or {}).get('PlaybackPositionTicks') or None
                self.play(item, server, offset_ticks=offset)
        self.run.run(work, done, ep)

    def shuffle_series(self, series_id, server):
        """Shuffle the whole show, like Tk's series-page Shuffle."""
        ep = self.run.epoch
        source = self.services.source

        def work():
            return [e.get('Id') for e in source.get_series_queue(server, series_id, limit=200) if e.get('Id')]

        def done(ids):
            if ids:
                self.play_shuffle(ids, server, audio=False)
        self.run.run(work, done, ep)

    def play_season(self, series_id, season_id, server):
        """Play one season from its first episode. The season queue, so
        missing and unaired episodes are left out (as in shuffle_season)."""
        ep = self.run.epoch
        source = self.services.source

        def work():
            return [e for e in source.get_season_queue(server, series_id, season_id) if e.get('Id')]

        def done(items):
            if items:
                self.play_list([e['Id'] for e in items], server, 0, items=items)
        self.run.run(work, done, ep)

    def shuffle_season(self, series_id, season_id, server):
        """Shuffle one season. The season queue, not the series one: it is
        season-scoped and carries web's filters that keep missing and
        unaired episodes out (repository.get_season_queue)."""
        ep = self.run.epoch
        source = self.services.source

        def work():
            return [e.get('Id') for e in source.get_season_queue(server, series_id, season_id) if e.get('Id')]

        def done(ids):
            if ids:
                self.play_shuffle(ids, server, audio=False)
        self.run.run(work, done, ep)

    def toggle_watched(self, item, server):
        from . import components
        ud = item.setdefault('UserData', {})
        was_played, was_count = (ud.get('Played'), ud.get('UnplayedItemCount'))
        was_pct = ud.get('PlayedPercentage')
        new = not components.is_watched(item)
        ud['Played'] = new
        if item.get('Type') in ('Series', 'Season', 'Folder'):
            ud['UnplayedItemCount'] = 0 if new else 1
            ud['PlayedPercentage'] = 100 if new else 0

        def work(ctl):
            if ctl.set_watched(server, item.get('Id'), new) is False:
                ud['Played'] = was_played
                if was_count is None:
                    ud.pop('UnplayedItemCount', None)
                else:
                    ud['UnplayedItemCount'] = was_count
                if was_pct is None:
                    ud.pop('PlayedPercentage', None)
                else:
                    ud['PlayedPercentage'] = was_pct
                self.services.invalidate()
        self._fire(work)
        self.services.invalidate()

    def toggle_favorite(self, item, server):
        ud = item.setdefault('UserData', {})
        was = ud.get('IsFavorite')
        new = not bool(was)
        ud['IsFavorite'] = new

        def work(ctl):
            if ctl.set_favorite(server, item.get('Id'), new) is False:
                ud['IsFavorite'] = was
                self.services.invalidate()
        self._fire(work)
        self.services.invalidate()

    def schedule_recording(self, item, server, series=False, on_done=None):
        """Record ``item`` (a Program), or its whole series."""
        program_id = item.get('Id')
        if not program_id:
            return
        done = _('Series recording scheduled.') if series else _('Recording scheduled.')

        def work(ctl):
            if series:
                ctl.create_series_timer(server, program_id)
            else:
                ctl.create_timer(server, program_id)
        self.edit(work, on_ok=lambda: self._recording_changed(done, on_done), error=_('The recording could not be scheduled.'))

    def cancel_timer(self, timer_id, server, on_done=None):
        """Cancel a scheduled recording (or stop one in progress), after
        confirming — this is destructive and one stray click away from a
        Record button."""
        if not timer_id:
            return
        self.dialogs.confirm(_('Cancel recording?'), lambda: self._do_cancel(lambda ctl: ctl.cancel_timer(server, timer_id), _('Recording cancelled.'), on_done), title=_('Cancel Recording'), yes=_('Cancel Recording'))

    def cancel_series_timer(self, series_timer_id, server, on_done=None):
        """Stop recording a series."""
        if not series_timer_id:
            return
        self.dialogs.confirm(_('Stop recording this series?'), lambda: self._do_cancel(lambda ctl: ctl.cancel_series_timer(server, series_timer_id), _('Series recording cancelled.'), on_done), title=_('Cancel Series'), yes=_('Cancel Series'))

    def _do_cancel(self, work, message, on_done):
        self.edit(work, on_ok=lambda: self._recording_changed(message, on_done), error=_('The recording could not be cancelled.'))

    def save_timer(self, server, timer_id, changes, series=False, on_done=None):
        """Apply an edited timer/series rule from the recording editor."""
        if not timer_id:
            return

        def work(ctl):
            if series:
                ctl.update_series_timer(server, timer_id, changes)
            else:
                ctl.update_timer(server, timer_id, changes)
        self.edit(work, on_ok=lambda: self._recording_changed(_('Recording settings saved.'), on_done), error=_('The recording settings could not be saved.'))

    def _recording_changed(self, message, on_done):
        self.services.set_status(message)
        if on_done is not None:
            on_done()
        self.services.invalidate()

    def can_record(self, server=None):
        """Whether the Record affordances should be offered.

        Two independent questions, and both have to say yes:

        1. Can this *apiclient* schedule a recording at all — the version
           probe ``live_tv_apis``, cached because it is a question about
           imported code and cannot change while we run.
        2. May this *user*, on this server. ``EnableLiveTvManagement`` is a
           third Live TV permission, separate from the access one the
           browse gate reads, and without it every Record button answers
           403 with a generic failure. Not cached here: it is per server
           and the answer already is cached, on the client.

        Fails OPEN in both, exactly as ``can_edit`` does and for the same
        reason: only a probe that positively answers False hides them. The
        API call is the real check.
        """
        if self._record_ok is None:
            try:
                answer = self.services.controller.live_tv_apis()
            except Exception:
                answer = None
            self._record_ok = answer is not False
        if not self._record_ok:
            return False
        source = getattr(self.services, 'source', None)
        ask = getattr(source, 'can_manage_live_tv', None)
        if ask is None or server is None:
            return True
        try:
            return bool(ask(server))
        except Exception:
            return True

    def can_manage_collections(self, server=None):
        """Whether the *collection* edit affordances should be offered.

        `can_edit` is the apiclient question and answers for playlists and
        collections together; this adds the half that is only about
        collections. `EnableCollectionManagement` gates the whole of
        `CollectionController` — create, add and remove are one permission —
        and a new account does not have it, so without this the Collections…
        button and Remove from Collection are offered to most users on a
        modern server and answer 403.

        Playlists are deliberately NOT gated with it: `PlaylistController`
        has no such policy, so a user who cannot touch a collection can still
        make a playlist, and hiding both would take away something that works.

        Fails OPEN in both halves, as `can_record` does: only a probe that
        positively answers False hides anything.
        """
        if not self.can_edit():
            return False
        source = getattr(self.services, 'source', None)
        ask = getattr(source, 'can_manage_collections', None)
        if ask is None or server is None:
            return True
        try:
            return bool(ask(server))
        except Exception:
            return True

    def can_refresh_metadata(self, server=None):
        """Whether to offer Refresh Metadata. Fails OPEN, like its siblings.

        The exception this repository tolerates rather than one it forgot:
        every other fail-open gate here hides a feature the user *might* not
        have, and this one can offer an administrator-only endpoint to a
        non-administrator whose policy we could not read. It needs two things
        at once and only one of them is common: `IsAdministrator` is in every
        policy on every server that has the endpoint, so this branch needs a
        failed policy fetch *and* a non-administrator. The failed fetch alone
        is ordinary -- `policy_for` answers `{}` after any exception from
        `get_user`, so a timeout, a 504 from a reverse proxy, an expired token
        or a malformed body all reach it. The conjunction is what makes it
        rare, not the state being impossible.

        `docs/PERMISSION_GAPS.md` §7 holds the reasoning and the trade; this
        says only enough to stop the next reader closing the gate.
        """
        source = getattr(self.services, 'source', None)
        ask = getattr(source, 'can_refresh_metadata', None)
        if ask is None or server is None:
            return True
        try:
            return bool(ask(server))
        except Exception:
            return True

    def refresh_metadata(self, item, server):
        """Ask the server to re-read this item's metadata.

        Reports rather than waits: the server queues the refresh and answers
        at once, so there is nothing to show progress for and nothing to
        reload -- what lands later arrives through the websocket item update
        the shell already listens for.
        """
        iid = item.get('Id')
        if not iid:
            return
        name = item.get('Name') or ''
        ctl = self.services.controller
        if ctl is None:
            return

        def queued():
            self.services.set_status(_('The server is refreshing %s.') % name)
        self.edit(lambda c: c.refresh_item(server, iid), on_ok=queued, error=_('%s could not be refreshed.') % name)

    def can_download(self, server=None):
        """Whether this user may fetch bytes from ``server`` at all.

        `EnableContentDownloading` is granted independently of everything
        else, and for books it is not a convenience gate: `/Items/{id}/
        Download` is the only endpoint that serves a book, so a user without
        it cannot read one. Every other download is an offline nicety, which
        is why this is asked about books and not bolted onto the generic
        download button -- taking that away from a user whose policy we
        could not read would be the worse mistake.

        Fails OPEN, like ``can_record`` and ``can_manage_collections``.
        """
        source = getattr(self.services, 'source', None)
        ask = getattr(source, 'can_download', None)
        if ask is None or server is None:
            return True
        try:
            return bool(ask(server))
        except Exception:
            return True

    def can_edit(self):
        """Whether the apiclient can edit playlists/collections.

        Fails OPEN: only a probe that positively answers False hides the
        affordances. An inconclusive probe (a controller without the method)
        hiding every edit control is a worse failure than showing one that
        errors — and the API call is the real check anyway.

        Cached: this is read from build(), i.e. once a repaint.

        Was ``TilesMixin._edit_apis``.
        """
        if self._edit_ok is None:
            try:
                answer = self.services.controller.edit_apis()
            except Exception:
                answer = None
            self._edit_ok = answer is not False
        return self._edit_ok

    @property
    def offline(self):
        """Whether there is a server to download from at all."""
        return self.services.offline

    def open_download(self, item):
        """Ask the shell for the download dialog (size estimate, options)."""
        self.dialogs.open_download(item)

    @staticmethod
    def can_delete(item):
        """Whether to offer Delete from Disk for this item.

        ``CanDelete`` off the DTO, which is jellyfin-web's own test
        (itemContextMenu.js) and the only correct one: the server computes
        it from the user's policy **and** the library the item is in, so
        deletion can be granted for one library and not another and no
        client-side reading of the account can know which.

        Absent means no, and that is the opposite of ``may_download``'s
        fail-open on purpose: hiding a delete the user could have made is an
        inconvenience, and offering one that 403s at the point of no return
        is not.
        """
        return bool((item or {}).get('CanDelete'))

    def confirm_delete_item(self, item, server, on_done=None):
        """Confirm, loudly, then delete this item from the server.

        The wording is jellyfin-web's ``ConfirmDeleteItem`` verbatim -- so it
        seeds into 86 locales -- and the name goes on its own line rather
        than into the sentence, which is both more legible and the only way
        it *can* seed (their placeholder is ``{0}`` and ours would be
        ``%s``, and the seeder compares placeholder shapes).

        "Delete from Disk" on the title and on the button, not "Delete":
        every other Delete on these screens removes a *download*, and the
        two are one careless press apart.
        """
        name = item.get('Name') or ''
        self.dialogs.confirm(name, lambda: self.delete_item(item, server, on_done=on_done), title=_('Delete from Disk'), yes=_('Delete from Disk'), detail=_('Deleting this item will delete it from both the file system and your media library. Are you sure you wish to continue?'))

    def delete_item(self, item, server, on_done=None):
        """Delete on the server, then drop any local copy.

        **The download goes too.** Leaving it behind means the library says
        the item is gone while this machine still plays it -- and the local
        file is now the only copy of something the user asked to destroy,
        which is a surprise in the more alarming direction. Best effort and
        after the fact: the server delete is the one that has to be
        reported, and a catalog row that failed to clear is a cosmetic
        problem next to it.

        ``on_done`` is how the *page* decides what to do next, because only
        it knows whether the deleted item was the thing on screen or a tile
        in a list.
        """
        iid = item.get('Id')
        if not iid:
            return
        name = item.get('Name') or ''

        def gone():
            ctl = self.services.controller
            try:
                if ctl is not None:
                    self._drop_downloads(ctl, item)
            except Exception:
                log.debug('could not drop the local copy of %s', iid, exc_info=True)
            self._on_downloads_changed()
            self.services.set_status(_('%s was deleted.') % name)
            if on_done is not None:
                on_done()
        self.edit(lambda c: c.delete_item(server, iid), on_ok=gone, error=_('%s could not be deleted.') % name)

    @staticmethod
    def _drop_downloads(ctl, item):
        """Delete whatever this item's *downloads* are, by type.

        The same dispatch as :meth:`remove_download`, shared so a container
        cannot be handled correctly in one place and by id in the other.
        """
        iid, kind = (item.get('Id'), item.get('Type'))
        if kind == 'Series':
            ctl.delete_download(series_id=iid)
        elif kind == 'Season':
            ctl.delete_download(series_id=item.get('SeriesId'), season_id=iid)
        elif kind == 'Playlist':
            from ..constants import split_offline_playlist_id
            real_id, offline_scope = split_offline_playlist_id(iid)
            ctl.delete_download(playlist_id=real_id, playlist_server_id=offline_scope or item.get('ServerId'))
        else:
            ctl.delete_download(item_id=iid)

    def confirm_remove_download(self, item):
        """Confirm, then delete this item's downloaded copy."""
        self.dialogs.confirm(_('Delete the downloaded copy of %s?') % item.get('Name', ''), lambda: self.remove_download(item), title=_('Delete Download'), yes=_('Delete'))

    def remove_download(self, item):
        """Delete this item's download, then refresh the badges."""
        iid, t = (item.get('Id'), item.get('Type'))
        ep = self.run.epoch
        ctl = self.services.controller

        def work():
            self._drop_downloads(ctl, item)

        def done(_ok):
            self._on_downloads_changed()

        def failed(_exc):
            self.services.set_status(_('The download could not be removed.'))
        self.run.run(work, done, ep, on_error=failed)

    def remove_downloads(self, item_ids, name=''):
        """Delete a named set of downloads, after confirming.

        An audiobook read from a folder is N catalog rows with no
        server-side object joining them — no series id, no playlist — so
        there is nothing for the scoped deletes to be scoped by. The caller
        names the rows instead.
        """
        ids = [i for i in item_ids or () if i]
        if not ids:
            return
        self.dialogs.confirm(_('Delete the downloaded copy of %s?') % name, lambda: self._do_remove_downloads(ids), title=_('Delete Download'), yes=_('Delete'))

    def _do_remove_downloads(self, ids):
        ep = self.run.epoch
        ctl = self.services.controller

        def work():
            ctl.delete_downloads(ids)

        def failed(_exc):
            self.services.set_status(_('The download could not be removed.'))
        self.run.run(work, lambda _r: self._on_downloads_changed(), ep, on_error=failed)

    def read_book(self, item, server):
        """Open a book in whatever the desktop reads it with, fetching it
        first if it is not on disk yet.

        Read is one gesture over two steps because a book cannot be read
        *from* the server — there is no endpoint that serves a page (see
        ``books.py``) — so downloading is never the thing the user wanted,
        only the thing that has to happen first. Download on its own is a
        separate button (:meth:`download_book`), for the case where it IS
        the thing they wanted.

        The wait is *not* a thread. Enqueuing registers the id here, and
        ``flush_pending_reads`` — driven by the catalog's own change
        notification — opens it when it lands. A poller per press would
        outlive the press, and there is already something that knows exactly
        when a download finishes.
        """
        self._book_fetch(item, server, then_open=True)

    def download_book(self, item, server):
        """Fetch a book and stop there.

        No download dialog, unlike every other Download button in the app:
        that dialog exists to confirm a size and to offer the watched
        filter, and a book has neither — no size on the wire at all, and
        nothing to expand. One file, one press.
        """
        self._book_fetch(item, server, then_open=False)

    def _book_fetch(self, item, server, then_open):
        iid = item.get('Id')
        if not iid:
            return
        ctl = self.services.controller
        if ctl is None:
            return
        ep = self.run.epoch
        name = item.get('Name') or ''

        def work():
            status, path = ctl.book_download_state(iid, server)
            if path:
                return ('open' if then_open else 'have', None)
            if not self.can_download(server):
                return ('forbidden', None)
            if self.services.offline:
                return ('offline', None)
            if status not in ('pending', 'downloading'):
                ctl.download_enqueue(server, iid, item.get('Type'), include_watched=True)
            return ('queued', None)

        def done(result):
            what, _extra = result
            if what == 'open':
                ok, _method = ctl.open_downloaded_file(iid, server)
                self.services.set_status(_('Opening %s…') % name if ok else _('Nothing on this system could open %s.') % name)
            elif what == 'have':
                self.services.set_status(_('%s is already downloaded.') % name)
            elif what == 'forbidden':
                self.services.set_status(_('Your account is not allowed to download from this server, and a book can only be read by downloading it.'))
            elif what == 'offline':
                self.services.set_status(_('%s has not been downloaded.') % name)
            else:
                if then_open:
                    self._pending_reads[iid] = (name, server)
                self.services.set_status(self.downloading_message(name))
            self._on_downloads_changed()
            self.services.invalidate()

        def failed(_exc):
            self.services.set_status(_('%s could not be downloaded.') % name)
            self._on_downloads_changed()
        self.run.run(work, done, ep, on_error=failed)

    @staticmethod
    def downloading_message(name):
        """The toast a queued book raises.

        One function rather than the format string in two places, because
        the *reader* clears it by comparing against it (#2) and a second
        spelling would silently never match.
        """
        return _('Downloading %s…') % (name or '')

    def clear_downloading_toast(self, item):
        """Take that toast down, now the thing it was about is on screen.

        ``TOAST_SECS`` is six, so on a local server the message outlives
        the download and sits over the first page of the book. Conditional
        on the text still being ours -- see ``clear_status_if``.
        """
        clear = getattr(self.services, 'clear_status_if', None)
        if clear is not None:
            clear(self.downloading_message(item.get('Name') if item else ''))

    def flush_pending_reads(self):
        """Open any book whose download has finished since the last call.

        Called from the downloads-changed hook, which means it runs on
        whichever thread the sync worker notifies from — so it touches only
        its own dict and the gateway, and reports through ``set_status``
        like everything else here.

        A failed download is dropped from the set with a message: left in,
        it would sit there being retried against a row that is never going
        to complete, and the user would be told nothing at all.
        """
        if not self._pending_reads:
            return
        ctl = self.services.controller
        if ctl is None:
            return
        for iid, (name, server) in list(self._pending_reads.items()):
            try:
                status, path = ctl.book_download_state(iid, server)
            except Exception:
                log.debug('could not read download state for %s', iid, exc_info=True)
                continue
            if path:
                if self._pending_reads.pop(iid, None) is None:
                    continue
                ok, _method = ctl.open_downloaded_file(iid, server)
                self.services.set_status(_('Opening %s…') % name if ok else _('Nothing on this system could open %s.') % name)
            elif status in (None, 'error'):
                if self._pending_reads.pop(iid, None) is None:
                    continue
                self.services.set_status(_('%s could not be downloaded.') % name)
        self.services.invalidate()
