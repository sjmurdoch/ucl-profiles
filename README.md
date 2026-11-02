# ucl-profiles

Exports everyone in a department on [UCL Profiles](https://profiles.ucl.ac.uk) (by default the Department of Computer Science) to an Excel table, so you can search and filter in Excel. The script doesn't filter anyone out. The columns are:

- **Name**, **Email** (from the profile: the main address, or else the first of its "other" addresses; the fake `nobody@ucl.ac.uk` that some profiles show counts as no address)
- **Missing InfComS**: Yes if the person doesn't have the "Information and Computing Sciences" tag (`required_tag`)
- **Missing area tag**: Yes if the person has none of the listed area tags (`area_tags`), such as "Machine learning" or "Cybersecurity and privacy"

- **Missing any tag**: Yes if either of the two columns above is Yes

Everyone needs both tags, so anyone with Yes in "Missing any tag" isn't set up properly. Matching ignores case.
- **Publications**: how many publications are linked to the profile, as a number
- **PGTA** and **Professional services**: guesses from job titles, since the site has no staff-category field. Each is Yes if every title matches one of its patterns (`pgta_positions` and `ps_positions`, case-insensitive regular expressions). So someone who is both a lecturer and a manager gets No
- **Positions**: job titles in the department. Where the site gives none, current appointment titles from the profile (e.g. "Student") are shown instead
- **Tags**, **Profile** (a clickable link)

A second sheet, "About", records when the export was made and the tags and patterns it used.

```sh
uv run ucl-profiles                                # writes ucl-profiles.xlsx
uv run ucl-profiles --output cs-tags.xlsx
uv run ucl-profiles --max-pages 1 --output test.xlsx  # quick test: first page (100 people) only
uv run ucl-profiles --format csv --output -        # same columns as CSV on stdout (or --format json)
uv run pytest                                      # tests (no network)
```

Looking up emails takes one request per person (about a minute for 565 people). Use `--no-emails` to skip it.

The server occasionally accepts a connection and never answers (a few percent of requests). Each request therefore times out after `--timeout` seconds (default 10) and, after a timeout, connection error, 429 or 5xx response, is retried up to `--retries` times (default 3) with exponential back-off (about 1s, 2s, 4s, plus random jitter). People whose profile still can't be fetched are listed with a warning and no email.

## Settings

Settings are applied in this order, with later ones taking priority: built-in defaults, then a TOML file (`ucl-profiles.toml` in the current directory, or `--config` / `UCL_PROFILES_CONFIG`), then environment variables (`UCL_PROFILES_<KEY>`), then command-line options (`--<key-with-dashes>`). See [`ucl-profiles.example.toml`](ucl-profiles.example.toml) for every key. List values in the environment are separated by `;`, because tag names contain commas. On the command line, repeat `--area-tag` once per tag.

Tags are matched without regard to case, because the site's capitalisation differs from the policy list (for example "Information and computing sciences"). If no one in the department has the required tag or one of the area tags, a warning is printed, since that usually means the tag name has a typo.

## The API

UCL Profiles runs Symplectic Discovery. Its JSON API is undocumented. The search page uses `POST https://profiles.ucl.ac.uk/api/users` with a body like:

```json
{
  "params": {"by": "text", "category": "user", "text": ""},
  "pagination": {"startFrom": 0, "perPage": 100},
  "sort": "relevance",
  "filters": [
    {"name": "tags", "matchDocsWithMissingValues": true, "useValuesToFilter": false},
    {"name": "department", "matchDocsWithMissingValues": false, "useValuesToFilter": true, "values": ["Dept of Computer Science"]}
  ]
}
```

The response contains `pagination.total`, a `resource` list of people (with `tags.explicit[].value`, `positions`, `discoveryUrlId`, `linkedObjectIds.publications[]` (absent when there are none), …) and `filters` with facet counts. `perPage` is capped at 100.

`GET https://profiles.ucl.ac.uk/api/users/{objectId}` returns a person's full profile. This includes `emailAddress.address` and `institutionalAppointments[]` (`position`, `institution`, `startDate`, `endDate`), which search results don't have.
