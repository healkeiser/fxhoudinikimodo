# Contributing

Thanks for taking the time. This is a bridge between two moving parts, NVIDIA Kimodo and
Houdini, so most of what follows is about which half a change belongs in and how to test
it without a GPU farm.

## Table of Contents

- [Where a change belongs](#where-a-change-belongs)
- [Getting set up](#getting-set-up)
- [The HDA is generated](#the-hda-is-generated)
- [Tests](#tests)
- [The panel rules](#the-panel-rules)
- [Style](#style)
- [Commits](#commits)
- [Pull requests](#pull-requests)
- [Licensing of contributions](#licensing-of-contributions)

## Where a change belongs

Three projects sit in a line, and a bug usually belongs to exactly one of them:

| Symptom | Where it goes |
| --- | --- |
| The motion itself is wrong, drifts, or ignores the prompt | [nv-tlabs/kimodo](https://github.com/nv-tlabs/kimodo), not here |
| The server does not start, or `/generate` returns the wrong thing | here, `kimodo_server.py` |
| The node, the panel, the skinning or the transforms | here, `scripts/create_hda.py` or `houdini/python/` |
| The containers do not come up | here, `docker-compose.bridge.yaml` |

If you are not sure, open an issue and say what you observed. Guessing wrong costs
nothing.

## Getting set up

The full walkthrough is [docs/setup.md](docs/setup.md); the short version is in the
README under [Installation](README.md#installation). You need both halves running before
you can test most changes:

```shell
docker compose -f docker-compose.bridge.yaml up text-encoder -d   # wait for "healthy"
MOCK_MODE=0 docker compose -f docker-compose.bridge.yaml up api -d
curl http://localhost:8001/health     # {"status":"ok","mock_mode":false}
```

`MOCK_MODE=1` serves a canned `dev_reference.npz` instead of running inference. Use it for
anything that is not the model itself: it needs no GPU, starts in seconds, and is the only
sane way to iterate on the panel or the node interface.

Do not `pip install` into Houdini's Python or your user site-packages. The one dependency
that does not ship with Houdini is QtPy, and it goes in `vendor/`, which the package file
puts on `PYTHONPATH`:

```shell
python -m pip install --target vendor --no-deps QtPy
```

## The HDA is generated

This is the rule that costs the most time when missed. `scripts/create_hda.py` is the
single source of the node interface, cook scripts and callbacks.
`houdini/otls/vb_kimodo_motion_1.1.hda/` is the expanded, reviewable result, and
`vb_kimodo_motion_1.1.hda` in the repo root is the packed one.

**Anything you change in Type Properties by hand is overwritten on the next rebuild.**
Fold it into the script instead, then regenerate:

```shell
hython scripts/build_skin.py     # embedded skin mesh + A-pose skeleton, needs kimodo cloned alongside
hython scripts/create_hda.py     # the HDA itself, packed, in the repo root
hython scripts/_add_help.py      # help card, saved expanded into houdini/otls/
```

Commit both the packed `.hda` and the expanded `houdini/otls/` tree. The expanded tree is
what a reviewer can actually read.

`.gitattributes` forces LF inside `houdini/otls/` and marks the gzipped section binary. If
your editor or a script rewrites those line endings, `hotl` cannot repack the HDA. Do not
"fix" the line endings.

## Tests

Two of the three need neither Houdini nor a GPU, so there is no excuse for skipping them:

```shell
python tests/test_timeline_model.py     # pure-python timeline model
python tests/test_panel_conventions.py  # source-level guards, see below
```

The third drives the real widgets and has to run inside a running Houdini, not `hython`,
because it needs `hou.ui` and a real Qt application:

```python
exec(open("tests/test_houdini_live.py").read()); print("\n".join(run()))
```

Run the live test for any change under `houdini/python/kimodo_timeline/`. It states its own
limits honestly at the top of the file: it proves the hygiene properties it can measure,
and it does not prove the pane-wedge bug is gone, because no reliable programmatic
detector for that was ever found. Clicking a Houdini pane afterwards is still part of the
job.

## The panel rules

`tests/test_panel_conventions.py` is not style policing. Every rule in it corresponds to a
bug that actually shipped and locked Houdini's panes out of the mouse. The recurring cause
is re-entering Houdini from inside a Qt handler: calling `QDialog.exec`, opening a dialog
from a mouse handler, or running work from a click handler rather than from the event
loop.

If that test fails, do not work around it. Read the rule it names and the commit it came
from.

## Style

- **ASCII only** in code, comments, log strings and commit messages. No smart quotes, no
  em dashes, no arrows. They break in Houdini's console and in the HDA sections.
- Match the file you are editing: its comment density, its naming, its idiom.
- Comments explain why, not what. The repo leans heavily on this, and it is why the
  reasoning behind an odd-looking workaround usually sits right above it.
- Say "segment", never "sequence". One concept, one word.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
<type>(<scope>): <description>
```

Types in use here: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `build`, `chore`.
Scopes in use: `hda`, `panel`, `server`, `compose`, `docs`, `tools`, or several separated
by commas when a change genuinely spans them (`fix(hda,panel):`). Append `!` for a
breaking change, as in `feat(hda)!:`.

Write a body when the change is not obvious from the subject. The history is deliberately
verbose about *why* a fix works, including the approaches that did not, so the next person
does not retry them.

## Pull requests

- Branch from `main`.
- Keep it to one concern. A rename and a bug fix in the same PR is two PRs.
- Say how you tested it. "Generated a 3-segment clip in Houdini 20.5, encoder on CPU" is
  worth more than a green checkmark.
- If you touched the HDA, confirm you regenerated it rather than editing Type Properties.

## Licensing of contributions

The bridge code follows the upstream project's terms: released for personal and research
use. The Kimodo weights are under the
[NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license)
and the text encoder's base weights under the Llama 3 Community License. By contributing
you agree your work ships under the same terms as the rest of the repository. Read the
[License](README.md#license) section before any use beyond personal testing.
