# Open WebUI Database Analyzer

Analyze `webui.db` SQLite database from [Open WebUI](https://github.com/open-webui/open-webui) to get insights on chat volume, user activity, model usage, and feedback compliance.

Tested with Open WebUI v0.6.30+.

## Features

- **Chat Volume**: Total chats, messages, per-user breakdown
- **User Statistics**: Activity, roles, last active times
- **Timeline Analysis**: Monthly, daily, hourly chat patterns
- **Model Usage**: Which AI models are being used
- **Feedback Analysis**: Thumbs up/down statistics with compliance tracking
- **Verification**: Compare against Open WebUI JSON export for accuracy

## Installation

No dependencies required - uses Python standard library only.

```bash
git clone https://github.com/terry-li-hm/open-webui-db-analyzer.git
cd open-webui-db-analyzer
```

## Usage

```bash
python analyzer.py <path_to_webui.db> [command] [options]
```

### Commands

| Command | Description |
|---------|-------------|
| `summary` | Overview + chat volume (default) |
| `chats` | Chat volume analysis |
| `users` | User statistics |
| `timeline` | Activity over time (monthly, daily, hourly) |
| `models` | Model usage breakdown |
| `feedback` | Thumbs up/down feedback analysis |
| `verify` | Data accuracy cross-checks |
| `compare` | Compare against Open WebUI JSON export |
| `export` | Export chat data to JSON |
| `all` | Run all analyses |

### Options

| Option | Description |
|--------|-------------|
| `--all-users`, `-a` | Show all users (default: hide users with <500 chats) |
| `--min-chats N`, `-m N` | Minimum chats to show user (default: 500) |
| `--export-file FILE`, `-e FILE` | Open WebUI feedback JSON export for comparison |
| `--output FILE`, `-o FILE` | Output file for export command |

## Examples

```bash
# Basic summary
python analyzer.py ~/webui.db

# Feedback analysis (key users only)
python analyzer.py ~/webui.db feedback

# Feedback analysis (all users)
python analyzer.py ~/webui.db feedback --all-users

# Feedback analysis (users with 100+ chats)
python analyzer.py ~/webui.db feedback -m 100

# Verify against Open WebUI export
python analyzer.py ~/webui.db compare -e feedback_export.json

# Full analysis
python analyzer.py ~/webui.db all

# Export chats to JSON
python analyzer.py ~/webui.db export -o my_chats.json
```

## Feedback Analysis

The feedback command provides detailed compliance tracking:

### Monthly Compliance
Shows per-month breakdown with thumbs up/down counts:
```
MONTHLY FEEDBACK COMPLIANCE
---------------------------------------------------------------------------
Month        Chats   No FB     👍     👎     Rate
---------------------------------------------------------------------------
2024-10        156      12    130     14    92.3%
2024-11        189      23    150     16    87.8%
2024-12        203       8    180     15    96.1%
```

### Per-User Monthly Compliance
Track if users are improving their feedback habits over time:
```
USER FEEDBACK COMPLIANCE BY MONTH
---------------------------------------------------------------------------
User                                Tot    24-10         24-11         24-12
                                          Rate  👍/👎   Rate  👍/👎   Rate  👍/👎
---------------------------------------------------------------------------
Alice Johnson                       523    85%  42/3     92%  55/4     95%  60/2
Bob Williams                        498    45%  20/5     60%  35/8     78%  48/6
```

### User Summary
```
USER FEEDBACK SUMMARY (All Time)
------------------------------------------------------------------------------------------
User                                     Chats   No FB     👍     👎     Rate
------------------------------------------------------------------------------------------
Alice Johnson                              523      12    480     31    97.7%
Bob Williams                               498      45    420     33    91.0%
```

## Verification

### Cross-check with Open WebUI Export

Export feedback from Open WebUI admin panel, then compare:

```bash
python analyzer.py webui.db compare -e feedback_export.json
```

```
VERIFICATION: Database vs Open WebUI Export
======================================================================

----------------------------------------------------------------------
COMPARISON
----------------------------------------------------------------------
Metric                             Export     Database      Match
----------------------------------------------------------------------
Total records                         567          567          ✓
Thumbs up (rating=1)                  423          423          ✓
Thumbs down (rating=-1)                89           89          ✓
Other/null ratings                     55           55          ✓
Unique chat IDs                       456          456          ✓
----------------------------------------------------------------------

✓ ALL METRICS MATCH - Database analysis is accurate!
```

### Internal Verification

```bash
python analyzer.py webui.db verify
```

Shows:
- Raw table counts (direct SQL)
- Actual rating values found in database
- Sample feedback records for spot-checking
- Cross-reference validation
- Consistency checks

## Getting webui.db

The database is located inside the Open WebUI Docker container:

```bash
# Copy from Docker container
docker cp open-webui:/app/backend/data/webui.db ./webui.db
```

Or if running locally, check the `data/` directory in your Open WebUI installation.

## Related

- [Open WebUI](https://github.com/open-webui/open-webui) - The web interface this tool analyzes
- [open-webui-feedback-analyzer](https://github.com/terry-li-hm/open-webui-feedback-analyzer) - Analyze exported feedback JSON files

## License

MIT
