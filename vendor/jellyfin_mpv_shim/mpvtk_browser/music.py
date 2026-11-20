"""Music browsing and the now-playing bar.

The music tabs (albums / artists / songs / genres / playlists), the album,
artist, genre and playlist views, and the persistent audio bar.

State on ``self``: ``_now_playing`` (the latest playstate snapshot, written
from a foreign thread by core's ``on_playstate``) and ``_np_thread`` (the
1s ticker that keeps the bar's clock moving). Paging
state lives in the route dict.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from ..i18n import _, _p
from ..mpvtk.widgets import Box, Column, Dropdown, Icon, Row, Slider, Text
from . import components, theme
NOW_PLAYING_BAR_H = 64
NOW_PLAYING_BAR_H2 = 96

def now_playing_bar_h(state, width):
    """How tall the bar will be for ``state`` at ``width``.

    A function rather than a constant because the layout is now a choice —
    and the two callers that subtract it (the content-height calculation
    and the cast screen) have to subtract what will actually be drawn, or
    the page is laid out against the wrong remaining height.
    """
    return NOW_PLAYING_BAR_H2 if MusicMixin.np_two_row(state or {}, width) else NOW_PLAYING_BAR_H

class MusicMixin:
    _duration = staticmethod(components.track_duration)
    ROUTES: dict = {}

    def _queue_items(self, ids, server, next_up=False):
        if next_up:
            self._actions.queue_next_items(ids, server)
        else:
            self._actions.queue_items(ids, server)

    @staticmethod
    def _fmt(secs):
        secs = int(secs or 0)
        return '%d:%02d' % (secs // 60, secs % 60)

    def _ctl(self, fn):
        if self.controller is not None:
            fn(self.controller)
    _REPEAT = ['none', 'all', 'one']

    @property
    def _REPEAT_TIPS(self):
        return {'none': _('Repeat all'), 'all': _('Repeat one'), 'one': _('Repeat off')}

    def _cycle_repeat(self):
        np = self._now_playing or {}
        cur = np.get('repeat', 'none')
        nxt = self._REPEAT[(self._REPEAT.index(cur) + 1) % 3] if cur in self._REPEAT else 'all'
        np['repeat'] = nxt
        self._ctl(lambda c: c.set_repeat(nxt))
        self.invalidate()

    def _toggle_np_favorite(self):
        np = self._now_playing or {}
        np['favorite'] = not np.get('favorite')
        self._ctl(lambda c: c.toggle_favorite())
        self.invalidate()

    def _np_scrub_change(self, v):
        """Track the drag so the elapsed clock follows the handle.

        No pause, unlike the video HUD: pausing to inspect a frame makes
        sense; pausing a song because you touched the seek bar does not.
        The seek itself still only fires on release."""
        self._np_scrub = float(v)
        self.invalidate()

    def _np_scrub_commit(self, v):
        self._np_scrub = None
        self._ctl(lambda c: c.seek(float(v)))
        self.invalidate()

    def _np_scrub_cancel(self):
        self._np_scrub = None
        self.invalidate()

    @staticmethod
    def _chapter_index(chapters, pos):
        """Which chapter ``pos`` falls in. The last one that has started —
        the same rule the video HUD's picker uses."""
        current = 0
        for i, chapter in enumerate(chapters):
            if chapter['time'] <= pos:
                current = i
        return current
    SKIP_BACK = 10
    SKIP_FORWARD = 30
    NP_BTN_W = 20 + 2 * 7 + 10
    NP_CLOCK_W = 2 * (48 + 10)
    NP_VOL_W = 20 + 10 + (110 + 10)
    NP_PICKER_W = 38 + 10
    NP_MIN_SEEK = 140
    NP_TWO_ROW_UNDER = 1100

    @staticmethod
    def np_two_row(np, w):
        """Whether the scrubber gets its own row above the controls.

        Always for an audiobook: a book is one long item and the scrubber
        is how you move around inside it, so it earns full width rather
        than whatever is left after eleven buttons. And for anything else
        on a narrow window, where the alternative is a scrubber a
        centimetre wide between two clocks.

        Static so the shell can ask how tall the bar will be before it
        builds one -- the page above has to be laid out against the height
        that will actually be drawn.
        """
        return bool(np.get('is_audiobook')) or w < MusicMixin.NP_TWO_ROW_UNDER

    def _np_plan(self, np, w):
        """Which optional controls this bar has room for.

        A *priority list* rather than a table of per-control width
        thresholds. Two reasons, and the second is the one that matters:

        * thresholds are guesses about a sum, and they were wrong — the
          bar fitted its own tier table at every width and still had a
          zero-width scrubber, because nothing checked the total;
        * what is worth dropping depends on what is playing. Chapter
          navigation outranks the favourite heart while a book is on and
          does not exist at all while a song is, so one fixed order cannot
          serve both.

        The longest PREFIX that fits is kept, not a greedy best-fit: a
        prefix is monotone in width, so narrowing the window only ever
        takes things away, and widening it only ever brings them back in
        the same order. A best fit would pop a cheap control in and out as
        an expensive one came and went.
        """
        book = bool(np.get('is_audiobook'))
        chapters = bool(np.get('chapters'))
        btn = self.NP_BTN_W
        order = [('clock', self.NP_CLOCK_W)]
        if book:
            order.append(('skip_btns', 2 * btn))
        order.append(('volbar', self.NP_VOL_W))
        if chapters:
            order.append(('chapters', self.NP_PICKER_W))
        order += [('queue', btn), ('stop', btn)]
        if book and chapters:
            order.append(('ch_btns', 2 * btn))
        order += [('favorite', btn), ('repeat', btn)]
        used = 2 * 10 + self._np_title_w(w) + 10 + btn
        if self.np_two_row(np, w):
            order = [(k, c) for k, c in order if k != 'clock']
        else:
            used += 2 * btn + self.NP_MIN_SEEK
        if not self._np_solo_chapters(np):
            used += 2 * btn
        plan = {key: False for key, _cost in order}
        plan['clock'] = plan.get('clock') or self.np_two_row(np, w)
        for key, cost in order:
            if used + cost > w:
                break
            used += cost
            plan[key] = True
        return plan
    NP_TITLE_W = 200

    @classmethod
    def _np_title_w(cls, w):
        return cls.NP_TITLE_W

    def _np_btn(self, icon, node_id, cb, tip, size=20, pad=7, color=None):
        """A square transport button, matching the bar's own row."""
        return Box([Icon(icon, size, color=color or theme.TEXT_FG)], id=node_id, pad=pad, bg=theme.BUTTON_BG, hover={'fill': theme.BUTTON_ACTIVE}, radius=6, align='center', direction='row', on_click=cb, tip=tip)

    def _transport(self, np, tiers):
        """The buttons around play/pause, in the playback HUD's order.

        ``prev | chapter- | -10s | play/pause | +30s | chapter+ | next``.
        That ordering is the HUD's and worth matching exactly: the two
        pairs nest around the middle button, so the further from the centre
        a control is, the bigger the jump it makes. Anyone who has used the
        video HUD already knows where to press.

        The chapter pair uses ``undo``/``redo`` for the same reason — those
        are the glyphs the HUD gives chapter navigation, and ``fast_rewind``
        /``fast_forward`` read as scanning, which is not what they do.
        """
        pp = 'play_arrow' if np.get('paused') else 'pause'
        chapters = bool(np.get('chapters'))
        book = bool(np.get('is_audiobook'))
        walk = not self._np_solo_chapters(np)
        out = [self._np_btn('skip_previous', 'np-prev', lambda: self._ctl(lambda c: c.prev()), _('Previous'))] if walk else []
        if book and chapters and tiers['ch_btns']:
            out.append(self._np_btn('undo', 'np-chprev', lambda: self._ctl(lambda c: c.chapter_seek(-1)), _('Previous chapter')))
        if book and tiers['skip_btns']:
            out.append(self._np_btn('replay_10', 'np-back', lambda: self._ctl(lambda c: c.seek_relative(-self.SKIP_BACK)), _('Back %d seconds') % self.SKIP_BACK))
        out.append(self._np_btn(pp, 'np-pp', lambda: self._ctl(lambda c: c.toggle_pause()), _('Play') if np.get('paused') else _('Pause')))
        if book and tiers['skip_btns']:
            out.append(self._np_btn('forward_30', 'np-forward', lambda: self._ctl(lambda c: c.seek_relative(self.SKIP_FORWARD)), _('Forward %d seconds') % self.SKIP_FORWARD))
        if book and chapters and tiers['ch_btns']:
            out.append(self._np_btn('redo', 'np-chnext', lambda: self._ctl(lambda c: c.chapter_seek(1)), _('Next chapter')))
        if walk:
            out.append(self._np_btn('skip_next', 'np-next', lambda: self._ctl(lambda c: c.next()), _('Next')))
        return out

    @staticmethod
    def _np_solo_chapters(np):
        """A chaptered item that is the whole queue.

        Which is what a single-file audiobook is: one `.m4b` IS the book,
        so previous/next track have nowhere to go — pressing either can
        only end playback. Worse, they sit immediately outside the chapter
        arrows, so the two pairs read as a set and half of them are traps.

        Deliberately narrow: it takes BOTH conditions. A rip's chapters are
        queue entries and prev/next are exactly right there, and a lone
        song without chapters keeps them because that is what the bar has
        always done for music.
        """
        return bool(np.get('chapters')) and (np.get('queue_len') or 1) <= 1

    def _chapter_picker(self, np, pos, tiers):
        """The chapter LIST, in the right-hand cluster with the other
        pickers — where the HUD keeps its own.

        It belongs there rather than among the transport buttons because it
        is not a step: it is a place to choose from, like the queue button
        it now sits beside. Wedged between the scrubber and the volume it
        also stranded the two chapter arrows away from the play button they
        step around.
        """
        chapters = np.get('chapters') or []
        if not chapters or not tiers['chapters']:
            return []
        labels = ['%s  %s' % (self._fmt(ch['time']), ch['title'] or _('Chapter %d') % (i + 1)) for i, ch in enumerate(chapters)]
        return [Dropdown('np-chapters', labels, selected=self._chapter_index(chapters, pos), force=True, trigger_icon='bookmark', tip=_('Chapters'), trigger_chip=(theme.BUTTON_BG, theme.BUTTON_ACTIVE, theme.TEXT_FG), w=38, h=38, popup_w=420, on_select=lambda i, v, chs=chapters: self._ctl(lambda c: c.seek(chs[i]['time'])))]

    def _now_playing_bar(self, w):
        np = self._now_playing
        tiers = self._np_plan(np, w)
        pos = np.get('position', 0) or 0
        scrub = self._np_scrub
        shown = pos if scrub is None else scrub
        dur = np.get('duration', 0) or 0
        repeat = np.get('repeat', 'none')
        seek = Slider('np-seek', value=pos, min=0, max=max(1, dur), force=True, flex=1, min_w=self.NP_MIN_SEEK, marks=[ch['time'] / dur for ch in np.get('chapters') or [] if 0 < ch['time'] < dur] or None, on_change=self._np_scrub_change, on_commit=self._np_scrub_commit, on_cancel=self._np_scrub_cancel)
        title = np.get('title', '')
        sub = np.get('artist') or np.get('album') or ''
        title_w = self._np_title_w(w)
        clock = [Text(self._fmt(shown), size='caption', w=48, color=theme.SUBTLE_FG)] if tiers['clock'] else []
        end = [Text(self._fmt(dur), size='caption', w=48, color=theme.SUBTLE_FG)] if tiers['clock'] else []
        right = []
        if tiers['stop']:
            right.append(self._np_btn('stop', 'np-stop', lambda: self._ctl(lambda c: c.stop()), _('Stop')))
        if tiers['favorite']:
            right.append(self._np_btn('favorite' if np.get('favorite') else 'favorite_border', 'np-fav', lambda: self._toggle_np_favorite(), _('Remove from favorites') if np.get('favorite') else _('Add to favorites'), color=theme.FAV_RED if np.get('favorite') else theme.TEXT_FG))
        right += self._chapter_picker(np, shown, tiers)
        if tiers['repeat']:
            right.append(self._np_btn('repeat_one' if repeat == 'one' else 'repeat', 'np-repeat', lambda: self._cycle_repeat(), self._REPEAT_TIPS.get(repeat, _p('playback control', 'Repeat')), color=theme.ACCENT if repeat != 'none' else theme.SUBTLE_FG))
        if tiers['volbar']:
            right += [Icon('volume_up', 20, color=theme.SUBTLE_FG), Slider('np-vol', value=np.get('volume', 100), min=0, max=100, w=110, tip=_('Volume'), force=True, on_change=lambda v: self._ctl(lambda c: c.set_volume(v, notify=False)), on_commit=lambda v: self._ctl(lambda c: c.set_volume(v)))]
        if tiers['queue'] and (not self.headless):
            right.append(self._np_btn('queue_music', 'np-queue', self._open_queue, _('Queue')))
        scrub_row = Row(clock + [seek] + end, gap=10, align='center')
        title_col = Column([Text(title, size='normal', bold=True), Text(sub, size='caption', color=theme.SUBTLE_FG)], gap=2, w=title_w)
        if not self.np_two_row(np, w):
            return Row([title_col] + self._transport(np, tiers) + clock + [seek] + end + right, pad=10, gap=10, align='center', h=NOW_PLAYING_BAR_H, bg=theme.PANEL_BG)
        return Column([scrub_row, Row([Box([title_col], flex=1, direction='row', align='center'), Row(self._transport(np, tiers), gap=10, align='center'), Box(right, flex=1, direction='row', align='center', justify='end', gap=10)], gap=10, align='center')], pad=(6, 10), gap=4, align='stretch', h=NOW_PLAYING_BAR_H2, bg=theme.PANEL_BG)
