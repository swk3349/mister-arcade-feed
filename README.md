# MiSTer arcade feed

A personal [Downloader](https://github.com/MiSTer-devel/Downloader_MiSTer) database that adds
third-party **arcade** FPGA cores (ones Update All doesn't carry) to a MiSTer, kept separate from
the proven cores.

- Games land in `_Arcade/_Beta/_Horizontal` and `_Arcade/_Beta/_Vertical`; core files in `_Arcade/cores`.
- Anything the official MiSTer, Jotego (incl. Patreon betas), Coin-Op or Arcade Offset databases already
  provide is dropped, so there are no duplicates. If a game is later adopted by one of those, the next build
  drops it here automatically.
- Games needing a light gun, wheel, yoke, trackball or handlebars are dropped (`exclude_titles` in `sources.json`).
- Files are fetched straight from each developer's GitHub, pinned to an exact commit. Nothing is re-hosted here.
- A GitHub Action rebuilds nightly and publishes to the `db` branch. `REPORT.md` there lists every game
  included and everything dropped with the reason.
- New core repos found on GitHub are listed in an issue titled **New arcade cores to review**. Nothing is added
  until you add it to `sources.json`.

## Install on the MiSTer

Add to the end of `/media/fat/downloader.ini`, then run Update All as normal:

```ini
[mister-arcade-feed]
db_url = https://raw.githubusercontent.com/swk3349/mister-arcade-feed/db/db.json.zip
```

## Everyday changes (all in `sources.json`, editable on github.com)

| To... | Do this |
|---|---|
| Add a core repo | Add `{"type": "github", "repo": "owner/name"}` to `sources` |
| Remove a core | Delete its line from `sources` |
| Hide a suggested repo | Add `"owner/name"` to `ignored_repos` |
| Allow a gun/wheel game | Delete its line from `exclude_titles` |

Saving `sources.json` on GitHub triggers a rebuild; the MiSTer picks it up on the next Update All run.

## Not covered (manual install)

Patreon-only cores (e.g. ikamusume's Cave CV1000, XelaNotPu's Namco System 12) can't be fetched
automatically. Copy their `.rbf` to `_Arcade/cores` and `.mra` files to `_Arcade/_Beta/_Horizontal` or
`_Vertical`. Downloader never deletes files it didn't install, so they're safe.

ROMs are not included: each game file names the MAME romset (zip) it needs in `games/mame`.
