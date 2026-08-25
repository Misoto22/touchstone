# Fan Out Fleet Configuration Through Pull Requests

A project describes the decisions that are the same across every repository it covers — which Loops run, on which engines, on what schedule — and lives in its own repository, so changing it is a reviewed pull request. `touchstone sync` renders one fleet-owned fragment per member; the member's own configuration names that fragment with `extends` and overrides any key it disagrees with. Precedence runs from machine-owned evidence, through the fleet's shared decisions, to the repository's own word, so the fleet proposes and the repository disposes.

A central process auditing every repository was rejected. It would concentrate the write access of a whole fleet into one credential domain and collapse the per-stage credential boundaries that separate the model from the publishing token, in exchange for saving a rendering step. Rendering keeps every member's state, Validation Gate authorization, and credential domain its own.

The fleet may not set `target`, `generated`, `project`, `state_dir`, or `version`. `target` carries Validation Gate authorization — which command a repository has agreed may run its own code — and moving that decision to whoever writes the central file takes it away from the repository it endangers. A credential-shaped key that is not an `op://` reference is refused outright, because a value written here would be rendered into every member it reaches.

`sync` has no direct-write path. The configuration file is the root of Touchstone's permission model: a mechanism able to edit it unattended could grant itself a Gate, so a fleet change arrives as a pull request and a person merges it.
