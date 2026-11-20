# Third-party notices

This distribution includes a modified Jellyfin MPV Shim source tree under
`vendor/jellyfin_mpv_shim`. Its original upstream is
https://github.com/jellyfin/jellyfin-mpv-shim, baseline commit
`daca187a9846192fa53d4c95f8eb5bb19aa9332b`.
It is not an official Jellyfin release. The original full license and MIT
attributions are retained in `LICENSE.md` and `vendor/JELLYFIN-MPV-SHIM-LICENSE.md`.
The combined derivative is distributed under GPL-3.0-or-later.

Local modifications include the cinema interface, media gateway, metadata and
cache workflows, configurable paths, encrypted profiles, and settings bridge.
`vendor/provenance.json` records source extraction hashes. These are provenance
records, not an assertion that the modified files equal upstream.
This release's modified distribution and attribution inventory were prepared
on 2026-11-20; the audit record is in `docs/implementation-ledger.md`.

`vendor/jellyfin_mpv_shim/ui_icon_paths.py` contains SVG path data derived from
[Google Material Design icons](https://github.com/google/material-design-icons).
Its original attribution docstring is retained. The path data was converted
into Python strings and is rendered by the customized UI; the complete Apache
License 2.0 is included in `vendor/licenses/APACHE-2.0.txt`, obtained from the
[upstream license](https://github.com/google/material-design-icons/blob/master/LICENSE).

The included Anime4K shader sources retain their copyright and license headers
and the shader pack's license files. Other bundled shaders retain their original
notices. Preserve these notices when redistributing.
The shader pack inventory is in
`vendor/jellyfin_mpv_shim/default_shader_pack/LICENSE.md`. Its KrigBilateral and
SSimDownscaler sources carry LGPL-3.0-or-later notices; the complete LGPL 3.0 text
is included in `vendor/licenses/LGPL-3.0.txt`, from the
[Free Software Foundation](https://www.gnu.org/licenses/lgpl-3.0.txt).
The GPL 3.0 text it supplements is already included in `LICENSE.md`.

The optional local anime face model under
`vendor/jellyfin_mpv_shim/mpvtk_browser/components/models` is from
[nagadomi/lbpcascade_animeface](https://github.com/nagadomi/lbpcascade_animeface),
copyright 2011 nagadomi@nurs.or.jp, under the MIT License. Its original license
is retained in the XML header, with the upstream README and source record beside
it. Photographic face detection can use the model supplied by an independently
installed OpenCV package; OpenCV is optional and is not bundled here.

Python dependencies are installed separately under their respective licenses.
MPV, Jellyfin Server, FFmpeg/ffprobe and OpenList executables are not bundled in
the source archive or wheel; their official distribution notices apply when
installed. User movie files, posters, credentials and personal databases are not
part of this software distribution. Third-party metadata is fetched only after
the user configures the relevant services; each source's usage terms still apply.
