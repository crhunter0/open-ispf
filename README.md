# Open ISPF TN3270 Server

A Python TN3270 server that emulates a classic z/OS login and ISPF flow over a normal TCP socket. It now includes a working Utility 3.4 (DSLIST) path with dataset browse/view/edit panels backed by a JSON catalog.

## Current Features

- TN3270 negotiation (BINARY, EOR, TERMINAL-TYPE) for standard 3270 emulators.
- TSO/E-style logon panel with RACF-style credential validation.
- ISPF Primary Option Menu and Utility Selection Panel.
- Utility option 4 (DSLIST) implementation under ISPF option 3.
- Dataset panels for B (Browse), V (View), and E (Edit) for sequential datasets.
- CP037/EBCDIC text handling for panel input/output and dataset decoding/encoding.
- Defensive parsing for malformed Telnet/TN3270 input (covered by robustness test).

## What Is Implemented Today

1. Connect with a TN3270 emulator to localhost:2323.
2. Log in through the TSO/E panel.
3. Open ISPF Utilities (option 3), then DSLIST (option 4).
4. Enter a DSN pattern (for example: TESTUSER.*).
5. Use line commands B, V, or E against listed datasets.
6. In dataset panels:
- PF7/PF8 scroll up/down.
- SCROLL PAGE or SCROLL CSR changes scroll mode.
- COLS toggles column ruler.
- HEX toggles hex display rows.
- Edit line command I inserts a blank line at the selected row.
- Edit line command D deletes the selected row.
- Edit block command DD marks a delete block (two DD markers) and deletes the range.
- Edit line command R replicates the selected row below itself.
- Edit line command C marks a source row and A/B applies copy After/Before a target row.
- Edit block command CC marks a block source (two CC markers) and A/B copies block After/Before target.
- Edit block command RR marks a block (two RR markers) and replicates it below the block.
- X, END, CANCEL, EXIT, or PF3 exits the panel.
- In Edit mode, PF3 or X path saves changes to disk.

## Screenshots

![TSO/E Logon Panel](docs/screenshots/logon_panel.png)
![ISPF Primary Option Menu](docs/screenshots/ispf_menu.png)

## Built-in Credentials

| Userid | Password |
|--------|----------|
| GP5CRH | TSYS |
| TESTUSER | RACF |

Credential checks are case-insensitive (values are normalized to uppercase before compare).

## Quick Start

### Prerequisites

- Python 3.8+
- A TN3270 emulator (x3270/wc3270 or equivalent)

### Run

```sh
python server.py
```

Default listener: 0.0.0.0:2323

### Connect

```sh
x3270 localhost:2323
```

or

```sh
wc3270 localhost:2323
```

## Configuration

Global settings are loaded from config.json:

```json
{
	"catalog_path": "catalog.json",
	"text_encoding": "cp037"
}
```

- catalog_path: path to the dataset catalog file.
- text_encoding: default text CCSID for panel/data text conversions.

If config.json is missing or invalid, safe defaults are used.

## Dataset Catalog

Datasets are defined in catalog.json under datasets. Example entry:

```json
{
	"dsn": "TESTUSER.DATA",
	"path": "data/TESTUSER/DATA.dat",
	"org": "PS",
	"recfm": "FB",
	"lrecl": 80,
	"content_mode": "text",
	"text_ccsid": "cp037"
}
```

Notes:

- Matching uses DSN wildcards via fnmatch semantics.
- Entries with org PO/POE are treated as PDS-like and currently return not implemented for member handling.
- content_mode binary is recognized but browse/save is not implemented yet.

## Project Layout

- server.py: TN3270 protocol handling, panel rendering, session loop.
- app_config.py: global config loading and path resolution.
- catalog_store.py: catalog loading/search and dataset file read/write helpers.
- ispf_utility_handlers.py: utility option dispatch.
- utilities/base.py: utility action/layout/result dataclasses.
- utilities/dslist.py: DSLIST and dataset panel interaction workflow.
- test_robustness.py: malformed input resilience regression test.

## Robustness

The robustness test covers malformed client sequences such as:

- lone IAC byte
- truncated IAC DO
- truncated IAC SB
- bare IAC EOR

Run it with:

```sh
python test_robustness.py
```

## Known Limitations

- Most ISPF primary options are placeholders.
- In Utilities, only option 4 (DSLIST) is implemented.
- PDS member listing/editing is not implemented yet.
- Binary dataset viewing/editing is not implemented yet.
- Current server loop handles one client at a time.

## References

- RFC 2355 - TN3270E
- IBM 3270 Data Stream Programming Reference
- x3270/wc3270 documentation

For educational and prototyping use.
