# Implementation contract — Personal Cinema

Product rules: [rules.md](rules.md). Current scope and limitations: [implementation-ledger.md](implementation-ledger.md). Jellyfin/MPV are foundations. Kazumi integration and new multi-version functionality are excluded. Builds contain source and application resources, never personal credentials, media or runtime data.

## Python package contracts
- Python >=3.11; standard library where feasible, unittest tests. Package src/personal_cinema; PYTHONPATH=src.
- schema.py: SECTIONS list of {id,label}; FIELDS list of {key,section,label,kind,default,help,scope,effect,options?,min?,max?}; options list {value,label}; schema() returns {sections,fields}. kind bool/int/float/text/path/select/list. Values keyed by dotted strings. Sections foundation,sources,organizing,metadata,artwork,ratings,playback,cache,network,appearance,automation,maintenance.
- configuration.py: ConfigStore(root).read() -> {version:1,revision:int,values:{...},overrides:{...}}; update(changes, expected_revision, scope='global')->same; export()->sanitized config; import_data(payload,expected_revision)->same. global means instance defaults; device/source:ID/item:ID/season:ID scopes. get_effective(scope='global') -> values. Errors ValueError, RevisionConflict. Atomic writes/thread safety. No secrets in config.
- credentials.py: SecretStore(root).set(name,value), get(name)->str|None, delete(name), status()->{name:bool}; DPAPI on Windows; tests inject reversible cipher, never plaintext fallback in production.
- integrations.py: IntegrationManager(root, config_store, secret_store).status()->list[{id,name,required,mode,state,detail,actions}]; perform(provider,action,payload)->dict. provider jellyfin/openlist/mpv/hypomux; actions probe/login/plan/apply/start/stop as actually implemented. Return only sanitized data, no secret echo. Tests only fake HTTP/processes. Owned-resource lifecycle must not stop user-owned services.
- Keys contracts: foundation.jellyfin_mode existing|managed; foundation.jellyfin_url http://127.0.0.1:8096; foundation.jellyfin_username ''; foundation.jellyfin_credential jellyfin.token; foundation.jellyfin_data ''; foundation.mpv_path ''; foundation.ffprobe_path ''; sources.openlist_mode disabled|existing|managed; sources.openlist_url http://127.0.0.1:5244; sources.openlist_username ''; sources.openlist_credential openlist.token; sources.entries []; network.mode direct|system|proxy; network.proxy_url ''; network.hypomux_path ''; network.preferred_interface ''; network.fallback direct|stop; maintenance.data_root ''; automation.mode manual|scheduled|startup; cache.playback_gib 10; cache.offline_gib 100; cache.image_gib 2; cache.reserve_gib 10; cache.directory ''. More fields as needed, notify other agents.

## Browser API contracts (root implements)
- GET /api/schema; GET /api/config; GET /api/status -> {integrations:[],runtime:{},setup:{}}.
- POST /api/config {revision,changes,scope?}; POST /api/config/import {revision,config}; GET /api/config/export.
- GET /api/credentials -> {configured:{name:bool}}; POST /api/credentials {name,value}; DELETE /api/credentials/{name}.
- POST /api/integrations/{provider}/{action} with provider-specific payload. Store credentials via credential route first unless login needs username/password directly; no logs or echo of password.
- POST /api/setup/complete; GET /api/diagnostics; POST /api/runtime/apply; POST /api/runtime/start; POST /api/runtime/stop; POST /api/runtime/launch-player; GET /api/tasks; POST /api/tasks/{action}.
- JSON errors {error:{code,message}}; HTTP409 revision conflict. Every mutation CSRF header X-Cinema-CSRF; GET /api/session returns {csrf} for authenticated browser. First launch URL fragment #token=...; JS POST /api/session {token} exchanges for HttpOnly same-site session, then removes fragment. Server loopback only and validates Host+Origin.
- Static files src/personal_cinema/web/{index.html,app.js,style.css}. No CDN. Header privacy and runtime scope clear. Existing cinema dark neutral rounded controls, no blue hover outline; keyboard focus visible. Wizard and searchable settings, save+reset+effect labels, credentials, integrations, import/export, diagnostics, runtime actions.

## Additional lifecycle interfaces
- `ConfigStore.effective_snapshot(scope)` returns one locked `{revision, values}` snapshot. Runtime application receipts must use that revision.
- `GET /api/organizing/plans`; `POST /api/organizing/plan|apply` expose per-source plans and configuration-bound approvals.
- `GET /api/maintenance/backups`; `POST /api/maintenance/backup|restore|prune|autostart` operate only on this instance. Restore takes `{id,revision}`.
- A settings-process OS lease prevents duplicate supervisors. Lifecycle mutations also hold a reentrant cross-process lock.
- The local launcher uses a DPAPI-protected handshake to reopen settings. This handshake and encrypted player-profile fragments are not editable provider credentials and are not returned to remote workbench clients.
- Installed-package verification is separate from source-tree unit tests: resources, authenticated settings and the gateway must run outside the checkout.
