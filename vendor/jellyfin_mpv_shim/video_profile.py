from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from .conf import settings
from .mpv_options import NAIVE_HWDEC, hwdec_pinned_by_config
from .shader_overrides import SCOPES, UNSET, ShaderOverrides, key_for
from . import conffile
from .utils import get_resource
from .constants import APP_NAME
from .i18n import _
import logging
import os.path
import shutil
import json
import sys
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .player import PlayerManager as PlayerManager_type
    from .menu import OSDMenu as OSDMenu_type
if sys.platform == 'win32':
    PLATFORM = 'windows'
elif sys.platform == 'darwin':
    PLATFORM = 'macos'
else:
    PLATFORM = 'linux'
profile_name_translation = {'Generic (FSRCNNX)': _('Generic (FSRCNNX)'), 'Generic High (FSRCNNX x16)': _('Generic High (FSRCNNX x16)'), 'Anime4K x4 Faithful (For SD)': _('Anime4K x4 Faithful (For SD)'), 'Anime4K x4 Perceptual (For SD)': _('Anime4K x4 Perceptual (For SD)'), 'Anime4K x4 Perceptual + Deblur (For SD)': _('Anime4K x4 Perceptual + Deblur (For SD)'), 'Anime4K x2 Faithful (For HD)': _('Anime4K x2 Faithful (For HD)'), 'Anime4K x2 Perceptual (For HD)': _('Anime4K x2 Perceptual (For HD)'), 'Anime4K x2 Perceptual + Deblur (For HD)': _('Anime4K x2 Perceptual + Deblur (For HD)'), 'ArtCNN (Denoise + Sharpen)': _('ArtCNN (Denoise + Sharpen)'), 'ArtCNN High (C4F32)': _('ArtCNN High (C4F32)')}
ASK_PLAYER = object()
SCOPE_LABELS = {'default': _('Default (all media)'), 'library': _('This Library'), 'series': _('This Series')}
log = logging.getLogger('video_profile')

def reset_saved_shader_settings():
    """Put the persisted shader settings back to their defaults.

    The two settings that can leave a machine unable to show video are the
    remembered profile (reapplied at startup by VideoProfileManager, before
    anyone can reach a menu to turn it off) and a forced graphics API. The
    ``k`` keybind clears the first, but that needs a visible window to press
    it in -- which is exactly what a bad gpu-api takes away. This is the same
    escape hatch from the command line.

    Returns ``[(key, old_value), ...]`` for what actually changed, so the
    caller can say what it did rather than claiming a reset that was a no-op.
    """
    changed = []
    if settings.shader_pack_profile is not None:
        changed.append(('shader_pack_profile', settings.shader_pack_profile))
        settings.shader_pack_profile = None
    if (settings.shader_pack_gpu_api or 'auto').lower() != 'auto':
        changed.append(('shader_pack_gpu_api', settings.shader_pack_gpu_api))
        settings.shader_pack_gpu_api = 'auto'
    if changed:
        settings.save()
    return changed

class MPVSettingError(Exception):
    """Raised when MPV does not support a required setting."""
    pass

def _hwdec_taken_out_of_our_hands():
    """Whether something outranks both us and the pack on hwdec.

    Two things do: the user's own mpv.conf (a pin -- nothing writes the
    option), and ``--disable-hwdec`` (the recovery path for hardware
    decoding stopping the window opening at all). A profile naming its
    decoder outranks the *setting*, but not these.
    """
    from .args import get_args
    try:
        if getattr(get_args(), 'disable_hwdec', False):
            return True
    except Exception:
        log.debug('could not read the hwdec override', exc_info=True)
    return hwdec_pinned_by_config() is not None

class VideoProfileManager:

    def __init__(self, menu: 'OSDMenu_type', player_manager: 'PlayerManager_type', player):
        self.menu = menu
        self.playerManager = player_manager
        self.used_settings = set()
        self.wants_copy_hwdec = False
        self._sets_vf = False
        self._names_direct_hwdec = False
        self.forced_hwdec = None
        self._suspended = None
        self.current_profile = None
        self.player = player
        self.profile_subtypes = []
        self.overrides = ShaderOverrides(conffile.get(APP_NAME, ShaderOverrides.FILENAME))
        self.active_scope = 'default'
        self._library_ids = {}
        self._menu_scope = 'default'
        self.session_default = settings.shader_pack_profile
        self.applied_settings = {}
        self.suppressed = False
        shader_pack_builtin = get_resource('default_shader_pack')
        self.shader_pack = shader_pack_builtin
        if settings.shader_pack_custom:
            self.shader_pack = conffile.get(APP_NAME, 'shader_pack')
            if not os.path.exists(self.shader_pack):
                shutil.copytree(shader_pack_builtin, self.shader_pack)
        pack_name = 'pack-next.json'
        if not os.path.exists(os.path.join(self.shader_pack, pack_name)):
            pack_name = 'pack.json'
            if not os.path.exists(os.path.join(self.shader_pack, pack_name)):
                raise FileNotFoundError('Could not find default shader pack.')
        with open(os.path.join(self.shader_pack, pack_name)) as fh:
            pack = json.load(fh)
            self.default_groups = pack.get('default-setting-groups') or []
            self.profiles = pack.get('profiles') or {}
            self.groups = pack.get('setting-groups') or {}
            self.revert_ignore = set(pack.get('setting-revert-ignore') or [])
            self.profile_subtypes = set()
            for profile in self.profiles.values():
                for subtype in profile.get('subtype', []):
                    self.profile_subtypes.add(subtype)
        self.defaults = {}
        for group in self.groups.values():
            setting_group = group.get('settings')
            if setting_group is None:
                continue
            for key, value in setting_group:
                if key in self.defaults or key in self.revert_ignore:
                    continue
                try:
                    self.defaults[key] = getattr(self.player, key)
                except Exception:
                    log.warning('Your MPV does not support setting {0} used in shader pack.'.format(key), exc_info=True)
        if settings.shader_pack_profile is not None:
            self.load_profile(settings.shader_pack_profile, reset=False)

    @staticmethod
    def profile_is_available(profile: dict) -> bool:
        """Whether this machine can run the profile at all.

        Gating is opt-in: a profile that says nothing about platforms runs
        everywhere, which is all of them but the few that had to declare
        themselves. Packs that predate this key are unaffected.
        """
        platforms = profile.get('platforms')
        return platforms is None or PLATFORM in platforms

    @staticmethod
    def api_setting_override(key: str, pack_value):
        """The two settings the pack uses to pin itself to OpenGL.

        `fbo_format: rgba16f` is an OpenGL-only spelling of the format MPV's
        `auto` already asks for portably, and the 2020 `gpu_api: opengl` was
        only ever there to make that spelling true. Both are dropped: the
        shaders do not need OpenGL, and forcing it costs HDR on Windows,
        where OpenGL is probed last.

        Only that one legacy value is refused — a profile naming some other
        API means it, and the user's `shader_pack_gpu_api` outranks both.
        Why, and what dumb mode has to do with it: `docs/mpv-backends.md` §11.

        Returns the value to apply, or None to leave the setting alone.
        """
        if key == 'fbo_format':
            log.debug("Ignoring shader pack fbo-format=%s; MPV's auto is portable.", pack_value)
            return None
        choice = (settings.shader_pack_gpu_api or 'auto').lower()
        if choice != 'auto':
            return choice
        if str(pack_value).lower() == 'opengl':
            log.debug("Ignoring shader pack gpu-api=opengl; leaving MPV's own choice.")
            return None
        return pack_value

    def process_setting_group(self, group_name: str, settings_to_apply: list, shaders_to_apply: list):
        group = self.groups[group_name]
        for key, value in group.get('settings', []):
            if key == 'hwdec':
                if _hwdec_taken_out_of_our_hands():
                    log.info("Not applying the shader pack's hwdec=%s; --disable-hwdec or mpv.conf decides it.", value)
                    continue
                if str(value).strip().lower() in NAIVE_HWDEC:
                    log.info("Not applying the shader pack's hwdec=%s; hardware decoding follows the Hardware Decoding setting.", value)
                    if not str(value).endswith('-copy'):
                        self._names_direct_hwdec = True
                    continue
                log.info('Shader profile requires hwdec=%s; applying it.', value)
                self.forced_hwdec = value
                self._names_direct_hwdec = not str(value).endswith('-copy')
            if key == 'vf':
                self._sets_vf = True
            if key in ('gpu_api', 'fbo_format'):
                value = self.api_setting_override(key, value)
                if value is None:
                    continue
            if key not in self.defaults:
                if key not in self.revert_ignore:
                    raise MPVSettingError('Cannot use setting group {0} due to MPV not supporting {1}'.format(group_name, key))
            else:
                self.used_settings.add(key)
            settings_to_apply.append((key, value))
        for shader in group.get('shaders', []):
            shaders_to_apply.append(os.path.join(self.shader_pack, 'shaders', shader))

    def load_profile(self, profile_name: str, reset: bool=True):
        if reset:
            self.unload_profile()
        log.info('Loading shader profile {0}.'.format(profile_name))
        if profile_name not in self.profiles:
            log.error('Shader profile {0} does not exist.'.format(profile_name))
            return False
        profile = self.profiles[profile_name]
        if not self.profile_is_available(profile):
            log.error('Shader profile {0} needs {1}, so it cannot run here.'.format(profile_name, '/'.join(profile['platforms'])))
            return False
        settings_to_apply = []
        shaders_to_apply = []
        self.wants_copy_hwdec = False
        self._sets_vf = False
        self._names_direct_hwdec = False
        self.forced_hwdec = None
        self.applied_settings = {}
        try:
            for group in self.default_groups:
                self.process_setting_group(group, settings_to_apply, shaders_to_apply)
            for group in profile.get('setting-groups', []):
                self.process_setting_group(group, settings_to_apply, shaders_to_apply)
            for shader in profile.get('shaders', []):
                shaders_to_apply.append(os.path.join(self.shader_pack, 'shaders', shader))
            already_set = set()
            for key, value in settings_to_apply:
                if (key, value) in already_set:
                    continue
                log.info('Set MPV setting {0} to {1}'.format(key, value))
                if key == 'gpu_api':
                    try:
                        setattr(self.player, key, value)
                    except Exception:
                        log.warning('MPV would not switch to gpu-api={0}; keeping the current one.'.format(value), exc_info=True)
                else:
                    setattr(self.player, key, value)
                already_set.add((key, value))
                self.applied_settings[key] = value
            self.wants_copy_hwdec = self._wants_copy()
            log.info('Set shaders: {0}'.format(shaders_to_apply))
            self.player.glsl_shaders = shaders_to_apply
            self.current_profile = profile_name
            self._reassert_user_settings()
            return True
        except MPVSettingError:
            log.error('Could not apply shader profile.', exc_info=True)
            return False

    def _client(self, client=ASK_PLAYER):
        """The server connection to ask about ``item``.

        The menus can take it from the player, because the item they are
        about is the one playing. **The play path cannot**: it applies the
        profile before ``_play_media`` assigns ``self._video``, so asking
        the player there answers with the PREVIOUS item's client -- which on
        a multi-server setup is a lookup against the wrong server. So it
        hands one in, and ``None`` from it is an answer ("there is no
        client") rather than an absence.
        """
        if client is not ASK_PLAYER:
            return client
        video = self.playerManager.get_video()
        return getattr(video, 'client', None) if video is not None else None

    def _library_id(self, item, client=ASK_PLAYER, force=False):
        """The CollectionFolder ``item`` lives in, or None.

        ``/Items/{id}/Ancestors`` is the only thing that answers this: an
        item DTO carries ``SeriesId`` but nothing naming its library, and
        the shim reaches items by search and by-name screens where there is
        no library in the route either. Measured at 15-19 ms against a local
        server, cached per `_library_key`, and -- see :meth:`scope_keys` -- not
        asked at all unless a library override exists to match.

        Keyed on the SERIES where there is one: every episode of a show is
        in the same library, so a whole series costs one request rather than
        one per episode.
        """
        key = self._library_key(item)
        if key is None:
            return None
        lookup = key[1]
        local = self._catalog_library_id(key)
        if local:
            self._library_ids[key] = local
            return local
        cached = self._library_ids.get(key, ASK_PLAYER)
        if cached is not ASK_PLAYER and (cached is not None or not force):
            return cached
        found = None
        try:
            client = self._client(client)
            if client is not None:
                for ancestor in client.jellyfin.get_ancestors(lookup) or []:
                    if ancestor.get('Type') == 'CollectionFolder':
                        found = ancestor.get('Id')
                        break
        except Exception:
            log.debug('could not resolve the library for %s', lookup, exc_info=True)
        self._library_ids[key] = found
        return found

    def _cached_library_id(self, item):
        """The library id **without asking the server** -- the local catalog
        (free, and right for anything downloaded) then the session cache."""
        key = self._library_key(item)
        if key is None:
            return None
        local = self._catalog_library_id(key)
        if local:
            self._library_ids[key] = local
            return local
        cached = self._library_ids.get(key)
        return cached if cached else None

    @staticmethod
    def _library_key(item):
        """``(ServerId, series or item id)`` for ``item``, or None if there
        is nothing to ask about.

        The one place the key is built, because it is both the cache key and
        the catalog scope and they must not drift apart. **The server half is
        load-bearing:** Jellyfin derives an item id from the media's path, so
        two servers over one library hand out identical ids -- and this cache
        lives for the session, so without it one server's library came back
        for another server's item with no lookup running at all.
        """
        item = item or {}
        lookup = item.get('SeriesId') or item.get('Id')
        return (item.get('ServerId'), lookup) if lookup else None

    @staticmethod
    def _catalog_library_id(key):
        """The library id the downloader recorded for this item or series on
        this server, or None. Never raises and never blocks on the network.

        The key's first half is already the Jellyfin ServerId, which is the
        catalog's content key -- so this hands it straight over. It used to
        translate it into a set of saved logins first, which was the same
        answer by a longer route.

        **A falsy ServerId answers for no rows, and that is intended.**
        `_content_clause` scopes a falsy value to nothing, so a key without a
        server gets no library id and the item simply has no library-scoped
        profile. The case it costs is narrow: every live server sends
        ``ServerId``, and `_add_row` has refused a DTO without one since
        ``ecfd316c``, so only a catalog row written before that can arrive here
        falsy. A download whose server was *removed* is **not** such a case --
        its row keeps the ``content_server_id`` it was written with, and its
        ``item_json`` still names the server -- so answering unscoped here
        would buy nothing and risk another server's library id through a
        colliding item id, which `_library_key` above calls load-bearing.
        Ruled 2026-09-19.
        """
        server_id, lookup = key
        try:
            from .sync.manager import syncManager
            db = getattr(syncManager, 'db', None)
            if db is None:
                return None
            return db.library_id(lookup, server_id=server_id)
        except Exception:
            log.debug("could not read the catalog's library id", exc_info=True)
            return None

    def scope_keys(self, item, force=False, client=ASK_PLAYER):
        """``{scope: storage key}`` for ``item``, for scopes that apply.

        A film has no series, so it has no ``"series"`` entry and the menu
        draws no such row -- "series → library → default" taken literally,
        which is what **[iw]** asked for ("setting per season would get
        annoying"; a per-film scope was not asked for and the library is
        already the right grain for one).

        ``force`` resolves the library even when no library override exists
        yet. The read path does not, because that lookup is a request; the
        *menu* must, because it is about to create the first one.

        **A cached answer counts as "already resolved".** The gate is about
        the cost of a *request*, not about whether the row is wanted -- and
        without that clause the HUD could never show a "This Library" row
        at all: it asks with ``force=False`` and warms the cache from the
        action thread, so a gate on ``has_any`` alone stays shut for exactly
        the user who has not made a library override yet, which is everyone
        who is about to make their first.
        """
        item = item or {}
        server = item.get('ServerId')
        keys = {}
        series_id = item.get('SeriesId')
        if series_id:
            keys['series'] = key_for(server, series_id)
        known = self._library_key(item) in self._library_ids
        if force or known or self.overrides.has_any('library'):
            library_id = self._library_id(item, client, force) if force else self._cached_library_id(item)
            if library_id:
                keys['library'] = key_for(server, library_id)
        return keys

    def resolve_for(self, item, client=ASK_PLAYER):
        """``(scope, profile)`` this item should play with."""
        return self.overrides.resolve(self.scope_keys(item, client=client), self._default_profile())

    def apply_for_item(self, item, client=ASK_PLAYER):
        """Put on whatever profile ``item`` resolves to.

        Replaces the bare :meth:`resume_after_still` on the play path: the
        answer for the next file is not necessarily the answer for the last
        one, and a still that suspended the profile is only the commonest
        reason for that rather than the only one.
        """
        if self.suppressed:
            if self.current_profile is not None:
                self.unload_profile()
            self._suspended = None
            return True
        scope, profile = self.resolve_for(item, client)
        self.active_scope = scope
        self._warm_library_later(item, client)
        if self._suspended is None and profile == self.current_profile:
            return True
        if scope != 'default':
            log.info('Shader profile %s comes from the %s override.', profile, scope)
        self._suspended = None
        if profile is None:
            if self.current_profile is not None:
                self.unload_profile()
            return True
        return self.load_profile(profile)

    def warm_library_scope(self, item, client=ASK_PLAYER):
        """Resolve this item's library id **now**, before playback starts.

        Called from ``play()``, which does not hold the player lock -- it is
        where PlaybackInfo is already fetched, so a request here costs the
        start nothing it was not already paying.

        This exists because the deferred version could not be right for every
        setting a profile carries. ``_warm_library_later`` lands the profile a
        beat into playback, and argues that is fine because "shader profiles
        are applied to a running mpv". True of ``glsl-shaders``; false of
        ``hwdec``, which mpv reads when the decoder is initialised -- so a
        library-scope profile naming a decoder was applied *after* the first
        item had already opened without it, and only the second item was
        right.

        Cheap and rare by construction: nothing happens unless a library
        override exists, and the id is cached (and comes free from the
        catalog for anything downloaded) after the first lookup.
        """
        try:
            if self.suppressed or not self.overrides.has_any('library'):
                return
            item = item or {}
            key = self._library_key(item)
            if key is None or key in self._library_ids:
                return
            self._library_id(item, client)
        except Exception:
            log.debug('could not resolve the library scope before playback', exc_info=True)

    def _warm_library_later(self, item, client):
        """Resolve this item's library **off the player lock**, if it still
        needs resolving, and re-apply once it is known.

        ``apply_for_item`` runs inside ``_play_media``, which holds the
        player's ``_lock`` for the whole of a playback start -- so a
        request there is exactly the thing not to do: ``run_action``'s
        non-blocking fast path is built on that lock being held, and the
        apiclient will retry an unresponsive server for about two and a
        half minutes. So the play path reads the caches only (the local
        catalog, then ``_library_ids``), and anything still unknown is
        resolved on the action thread.

        The profile then lands a beat into playback rather than before it,
        which is fine for the one thing this is: shader profiles are
        applied to a *running* mpv and switching one mid-playback is what
        the menu does. It only happens at all when a library override
        exists and the item is neither downloaded nor already cached.
        """
        if not self.overrides.has_any('library'):
            return
        key = self._library_key(item)
        if key is None or key in self._library_ids:
            return
        put_task = getattr(self.playerManager, 'put_task', None)
        if put_task is None:
            return

        def work():
            if self._library_id(item, client) is not None:
                self.apply_for_item(item, client)
        try:
            put_task(work)
        except Exception:
            log.debug('could not schedule the library lookup', exc_info=True)

    def scope_rows(self, item, force=True):
        """``[(scope, key, profile, is_set)]`` for the scope menu, narrowest
        first and always ending in ``"default"``.

        ``force=True``: the menu is where an override is created, so it has
        to know the library id before one exists. The **HUD** passes False
        and warms the cache from the action thread instead (see
        ``osc_bridge``), because its state blob is built on the render path
        and a 15-19 ms request does not belong there.
        """
        keys = self.scope_keys(item, force=force)
        rows = []
        for scope in SCOPES:
            key = keys.get(scope)
            if key is None:
                continue
            found = self.overrides.get(scope, key)
            is_set = found is not UNSET
            rows.append((scope, key, found if is_set else None, is_set))
        rows.append(('default', None, self._default_profile(), True))
        return rows

    def _default_profile(self):
        """What the default scope holds.

        ``session_default``, which is seeded from the remembered setting and
        then tracks what the user picks at that scope whether or not it is
        being persisted. Reading ``shader_pack_profile`` directly is what
        made "Remember Last Used Profile" off mean "the default scope is
        permanently None" -- misreported in the menu, and, worse, acted on
        by the re-resolve after a pick.
        """
        return self.session_default

    def set_scope_profile(self, item, scope, profile):
        """Write ``profile`` at ``scope``, then re-resolve and apply.

        Re-resolving rather than loading what was picked: setting the
        *library* while a *series* override is in force must not change what
        is on screen, or the menu lies about which scope wins.
        """
        self.suppressed = False
        if scope == 'default':
            self.session_default = profile
            if settings.shader_pack_remember:
                settings.shader_pack_profile = profile
                settings.save()
        else:
            keys = self.scope_keys(item, force=True)
            self.overrides.set(scope, keys.get(scope), profile)
        return self.apply_for_item(item)

    def clear_scope(self, item, scope):
        """Drop ``scope``'s override so the item inherits again."""
        self.suppressed = False
        if scope == 'default':
            return False
        keys = self.scope_keys(item, force=True)
        self.overrides.clear(scope, keys.get(scope))
        return self.apply_for_item(item)

    def suspend_for_still(self):
        """Take the shader profile off while a still image is on screen.

        A shader pack is applied once -- from the menu, or restored at
        startup from ``shader_pack_profile`` -- and then left on the mpv
        instance. Nothing on the play path touched it, so an anime-upscaling
        chain ran over a photograph, and over a comic page at 1600x2400 or
        larger, where it is both wrong and expensive: these packs are built
        for moving pictures at broadcast resolutions and a scanned page is
        neither.

        **Not ``unload_profile``**, which is the whole reason this exists:
        that clears ``current_profile``, and the menu's selection and
        ``menu_handle``'s persistence both read it -- so opening a photo
        would have silently reset the user's chosen profile. This is a
        suspension: mpv is put back to defaults, the *name* is kept, and
        :meth:`resume_after_still` puts it back.

        Idempotent. Photos arrive in queues, and every one of them runs the
        play path.
        """
        if self._suspended is not None or self.current_profile is None:
            return
        self._suspended = self.current_profile
        log.info('Suspending shader profile %s for a still image.', self._suspended)
        name = self.current_profile
        self.unload_profile()
        self.current_profile = name

    def resume_after_still(self):
        """Put back a profile :meth:`suspend_for_still` took off.

        A no-op when nothing was suspended, which is the ordinary case: this
        runs on every playback start, and almost none of them follow a
        still.
        """
        name, self._suspended = (self._suspended, None)
        if name is None:
            return
        log.info('Restoring shader profile %s.', name)
        self.load_profile(name, reset=False)

    def _wants_copy(self):
        """Does this profile need frames in system RAM?

        Only a real ``vf`` does -- a glsl shader runs inside the GPU
        renderer, on frames that are already on the GPU. So the pack's
        blanket ``hwdec: auto-copy`` is not the question; whether the
        profile installs a filter is.

        And a profile that names a **direct** hwdec mode alongside its
        filter is saying the opposite: its filter wants GPU frames. In the
        shipped pack that is exactly ``rtx-vsr``, whose
        ``format=nv12,d3d11vpp=scale=2:scaling-mode=nvidia`` is a Direct3D
        video-processor filter that operates on d3d11 surfaces --
        copying back would break the only profile in the pack that has a
        filter at all.
        """
        return self._sets_vf and (not self._names_direct_hwdec)

    def unload_profile(self):
        log.info('Unloading shader profile.')
        self.wants_copy_hwdec = False
        self._sets_vf = False
        self._names_direct_hwdec = False
        self.forced_hwdec = None
        self.player.glsl_shaders = []
        for setting in self.used_settings:
            value = self.defaults[setting]
            try:
                setattr(self.player, setting, value)
            except Exception:
                log.warning('Default setting {0} value {1} is invalid.'.format(setting, value))
        self.current_profile = None
        self.applied_settings = {}
        self._reassert_user_settings()

    def _reassert_user_settings(self):
        """Give the preset-driven settings the last word after a profile
        load or unload.

        The pack and these settings write some of the same properties --
        ``deband`` above all, which every profile turns on through
        ``default-setting-groups`` and every unload turns back off. The
        settings are the user's explicit answer and the pack's are a bundle
        that came along with picking an upscaler, so the settings win; a
        setting left at "off" writes nothing and the pack's value stands.

        Guarded rather than trusted: the profile has already been applied by
        the time this runs, and failing to reassert a preference must not
        turn a working profile load into an error the menu reports.
        """
        try:
            self.playerManager.reapply_render_presets()
        except Exception:
            log.debug('could not reassert the picture settings', exc_info=True)

    def profile_label(self, profile_name):
        """What a profile is called on screen, or "not set"/"none" for the
        two answers that are not a profile."""
        if profile_name is UNSET:
            return _('Not set')
        if profile_name is None:
            return _('None (Disabled)')
        profile = self.profiles.get(profile_name)
        name = profile['displayname'] if profile else profile_name
        return profile_name_translation.get(name, name)

    def _menu_item(self):
        video = self.playerManager.get_video()
        return getattr(video, 'item', None) or {} if video is not None else {}

    def menu_handle(self):
        """A profile was picked, for whichever scope the scope menu chose."""
        profile_name = self.menu.menu_list[self.menu.menu_selection][2]
        item = self._menu_item()
        scope = self._menu_scope
        if profile_name is UNSET:
            self.clear_scope(item, scope)
        else:
            self.set_scope_profile(item, scope, profile_name)
        self.menu.menu_action('back')
        self.menu.menu_action('back')
        self.menu_action()
        self.profile_menu(scope)

    def scope_handle(self):
        """A scope was picked: show that scope's profile list."""
        self.profile_menu(self.menu.menu_list[self.menu.menu_selection][2])

    def menu_action(self):
        """The scope step, which is also the report.

        **[iw]**: "a Default, Library Specific, Series Specific option from
        the menu before we show the options, and also show which of those is
        currently in effect". The second half is what makes it usable -- a
        film that is being sharpened differently otherwise has no visible
        cause -- so each row carries its own value and the winning one is
        marked.

        Only ever the scopes that apply: a film has no series row.
        """
        item = self._menu_item()
        rows = self.scope_rows(item)
        in_effect, _profile = self.overrides.resolve({scope: key for scope, key, _p, _s in rows if key}, self._default_profile())
        options = []
        selected = 0
        for scope, _key, profile, is_set in rows:
            label = '%s  ·  %s' % (SCOPE_LABELS.get(scope, scope), self.profile_label(profile if is_set else UNSET))
            if scope == in_effect:
                label = _('{0}  <-- in effect').format(label)
                selected = len(options)
            options.append((label, self.scope_handle, scope))
        self.menu.put_menu(_('Video Playback Profile'), options, selected)

    def profile_menu(self, scope):
        """The profile list, writing to ``scope``."""
        self._menu_scope = scope
        item = self._menu_item()
        keys = self.scope_keys(item, force=True)
        current = self._default_profile()
        if scope != 'default':
            found = self.overrides.get(scope, keys.get(scope))
            current = found
        options = []
        selected = 0
        if scope != 'default':
            options.append((_('Use the default'), self.menu_handle, UNSET))
            if current is UNSET:
                selected = 0
        options.append((_('None (Disabled)'), self.menu_handle, None))
        if current is None:
            selected = len(options) - 1
        for profile_name, profile in self.profiles.items():
            if profile.get('subtype', None) is not None and (not settings.shader_pack_subtype in profile['subtype']):
                continue
            if not self.profile_is_available(profile):
                continue
            name = profile['displayname']
            if name in profile_name_translation:
                name = profile_name_translation[name]
            options.append((name, self.menu_handle, profile_name))
            if profile_name == current:
                selected = len(options) - 1
        self.menu.put_menu('%s: %s' % (_('Select Shader Profile'), SCOPE_LABELS.get(scope, scope)), options, selected)
