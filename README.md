# animebay-catalog

The source list AnimeBay downloads at runtime. Editing `catalog.json` here
changes the sources for every install, with no new APK.

| Key | Meaning |
| --- | --- |
| `aniyomi` | Aniyomi extension repo URLs (each ends in `index.min.json`) |
| `cloudstream` | CloudStream repo URLs (`repo.json` / `plugins.json`) |
| `maxPerBackend` | How many English sources to install per backend |
| `testQuery` | Title the Phase 0 health check searches for |

The app reads:
`https://raw.githubusercontent.com/yahiaslimani/animebay-catalog/main/catalog.json`

## Disclaimer

AnimeBay does not host, upload or distribute any video. It claims no ownership
of any content it shows. All content belongs to its respective owners, and
credit goes to the extension authors and the sites they connect to. AnimeBay
only loads third-party extensions listed here. To request removal of an entry,
open an issue.
