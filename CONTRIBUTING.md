# Contributing

Bug reports, fixes and tests are all welcome. You don't need a camera for most of it, thanks to the simulator.

## Setting up

```bash
git clone https://github.com/ADCDS/quak.git && cd quak
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui,e2e,test]'
python -m unittest discover -s tests -t .
```

`QUAK_SIM=1 quak-gui` runs the app against the simulated camera. [docs/architecture.md](docs/architecture.md) explains how the code is
organised.

## Before you open a pull request

- Add a test for what you changed. Unit tests must pass on Linux and Windows.
- Run the simulator suites if you changed behaviour: `python -m quak.e2e --sim` and `python -m quak.e2e.gui --sim`.
- For a change to the app's screens, look at the result at phone size and at desktop size: `tools/screenshots.sh`.

## Claims about hardware

Please say what you really tested. A claim that something works on a camera needs the model, the firmware and what you did. When
the simulator and a real camera disagree, the camera wins: correct the simulator, and update its `ASSUMPTIONS` table.

Testing on other cameras, on Windows, or on other Android phones is especially useful, because Quak has only been tested on one
of each so far.

## Keeping private things out

Don't commit logs, packet captures or screenshots from a real camera: they can hold addresses, serials and network names. Take
screenshots with `tools/make_images.py`, which uses the simulator. `tests/test_safety.py` checks for the most common mistakes.

The test suites should only ever delete clips they recorded, and `tests/test_safety.py` keeps dangerous camera commands, such as
formatting a card, out of the code.

## Translations

The app's text lives in `src/quak/locale/`, one file per language, and the translated pages in `docs/i18n/`. They were
machine-translated and haven't been reviewed by native speakers, so corrections are very welcome: just edit the file. The camera's
own menu names are the most useful thing to check, because they have to match what the camera shows in that language.

In the code, text goes through `tr("Delete {name}?", name=name)`, or `trn("{n} clip", "{n} clips", count)` for counts, always as
one whole sentence, because word order changes between languages. Then run `python tools/i18n_sync.py` to add it to the catalogs. A
new message shows in English until it's translated, but the tests want every language complete before merging.

To add a language, add it to `LANGUAGES` in `src/quak/i18n.py`, add its plural rule there if it's unusual, run the sync tool and fill in
the new file. To see the app in a language without changing your settings, set `QUAK_LANG=de`. The command line and logs stay in
English on purpose, so scripts keep working and bug reports stay readable.

## Making a release

1. Set the new version in `src/quak/__init__.py`, `app/pyproject.toml` and `extensions/quak_native`, and add a section for it to `CHANGELOG.md`. The
   section becomes the release notes, so use full links in it.
2. Run `gh workflow run build.yml` and try the packages it builds.
3. Tag the commit: `git tag v1.2.3 && git push origin v1.2.3`.

The tag runs the `build` workflow, which builds everything again, publishes a GitHub Release and uploads to PyPI through trusted
publishing, using the `pypi` environment. No PyPI password or token is stored in the repository.

The APK is signed with a key held in the repository secrets `ANDROID_KEYSTORE_BASE64` and `ANDROID_KEYSTORE_PASSWORD`. Keep a
backup of it outside GitHub: without that key, a new APK can't update an installed one, and everyone has to uninstall first.

## How this project is built

Most of the code so far was written with AI assistance. Contributions made that way are fine, as long as you've checked them and can
explain them.
