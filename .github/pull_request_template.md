## What this changes

<!-- One or two sentences. If it fixes an issue, "Fixes #123". -->

## Why

<!-- The reasoning, including approaches that did not work if you tried any. The history
here is deliberately verbose about why, so the next person does not retry a dead end. -->

## How it was tested

<!-- Be specific. "Generated a 3-segment clip in Houdini 20.5, encoder on CPU" is worth
more than a green checkmark. Say if you could not test something. -->

- [ ] `python tests/test_timeline_model.py`
- [ ] `python tests/test_panel_conventions.py`
- [ ] `tests/test_houdini_live.py` in a running Houdini (required for panel changes)
- [ ] Clicked around Houdini's panes afterwards, they still take the mouse

## Checklist

- [ ] Commits follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
- [ ] ASCII only in code, comments, log strings and commit messages
- [ ] If the HDA changed: regenerated with `hython scripts/create_hda.py` rather than
      edited in Type Properties, and both the packed `.hda` and the expanded
      `houdini/otls/` tree are committed
- [ ] One concern per PR
