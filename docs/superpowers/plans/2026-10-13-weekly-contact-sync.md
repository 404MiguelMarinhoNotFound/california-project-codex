# Weekly Contact Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WhatsApp and phone calls resolve names against a snapshot of the phone's own contacts. The snapshot is refreshed over ADB at boot when it is more than 7 days old, then weekly, and on demand by voice. `contacts.vcf` stays as the fallback.

**Architecture:** A new `services/contact_sync.py` (`ContactSync`) runs one `content query` against the phone's Contacts provider through `AdbPhone`, folds the rows into `Contact` objects, diffs them against the previous snapshot, and writes `phone_contacts.json` atomically. `WhatsAppService._load_contacts` reads that snapshot first and the VCF second. `PhoneService` gets the new book for free, because it already resolves through `WhatsAppService.resolve_contact`. A background loop in the orchestrator, in the style of `_whatsapp_groups_loop`, decides when a sync is due. The voice path is one new `control_phone` action.

**Tech Stack:** Python 3.14, stdlib only (`json`, `os.replace`, `threading`), `adb` through the existing `AdbPhone` runner. uv only: `PYTHONIOENCODING=utf-8 uv run python -m unittest discover -s tests`.

**Spec:** the agreed decisions are in the "Feature" brief this plan was written from, and summarised in Global Constraints below. Background: `docs/ideas/2026-10-13-phone-toolkit.md` idea 9 ("Live contacts"), and CLAUDE.md "WhatsAppService" and "PhoneService".

## What the phone returned (probed read-only, 2026-10-13)

The command was `content query --uri content://com.android.contacts/data/phones --projection ...` on the Xiaomi at `192.168.1.101:<port>`. Results:

- **845 rows, 418 distinct `contact_id`, 1.4s.** One row per (raw contact, number). The same person appears once per account: `com.google` 429, `com.whatsapp` 339, `com.google.android.apps.tachyon` 73, `com.android.contacts.usim` 4. The WhatsApp and Duo/Meet rows are copies of the Google contact's numbers, so they **must be deduplicated** per `contact_id` after normalising.
- Row format: `Row: <n> key=value, key=value, ...`. Values are printed raw: no quoting, `NULL` for null, CRLF line ends over `adb shell`. Decoded as UTF-8 it is clean: 88 names were non-ASCII and 28 had emoji. This removes the quoted-printable problem the VCF has.
- No name contained `,` or `=` on this phone. **That is not guaranteed:** names are free text, and `data1` (the number) may legally contain `,` and `;`, which Android uses as dial pause and wait characters. Parsing therefore cannot split on `", "`. It must anchor on the known key sequence and put the one free-text column, `display_name`, **last** (Task 1).
- `data1` comes in many shapes: `+351912345678`, `912345678`, `912 345 678`, `+351 912 345 678`, `+44 (0) (20) 7946 0000`. `normalize_phone` already handles every one of them once the pause/wait suffix is cut.
- `data2` (type): 2 mobile 591, 0 custom 167, 1 home 78, 3 work 4, 12 main 4, 5 other-fax 1. `is_super_primary` is the "default number" flag.
- A contact with no name gets its number as `display_name`. `score_contacts` copes with that: it is a valid card that no spoken name will match.

**Why a shell `content query` and not a `callaudio` helper mode.** The shell user already holds `READ_CONTACTS`, the query is one round trip and takes 1.4s, and it needs neither the helper push nor the sha256 launch. A helper mode would add a dex rebuild, a hash bump and a `HELPER_MISMATCH` failure path, all to get JSON-escaped output. Parsing anchored on known keys gives the same safety. Revisit only if a real name ever breaks the parser, which Task 1's `unparsed` count will show.

## Global Constraints

- A weekly **snapshot**: lookups never touch the phone. Calls and WhatsApp keep working with the phone off the network.
- Sync at boot when the last sync is older than **7 days**, then weekly. The loop is stopped by `_background_stop`. Voice: "refresh my contacts".
- **Read-only on the phone:** `content query` only. Never `insert`, `update`, `delete`, push or settings.
- Snapshot file `phone_contacts.json` is gitignored. `contacts.vcf` remains the fallback.
- Logs carry **counts only** (`+added -removed ~changed`), never a name or a number. The same holds for the tool result and the spoken line.
- Windows only (`sys.platform == "win32"`), like `PhoneService`.
- The contact roster is still **never** injected into the system prompt (`test_llm_prompt.py` guard stays green).
- Every new tool-schema word is re-sent each turn: one enum value plus one sentence, nothing else.
- uv only. Docs: CLAUDE.md, never AGENTS.md.
- Tests never touch a real phone: every adb call goes through an injected `runner` (the `FakeAdb` pattern in `tests/test_phone_adb.py`).

## Review Focus

1. **The phone answers but returns a tiny or empty book.** This happens when the phone is locked down, the permission is revoked, or a provider hiccups. Expected: the old snapshot is kept and he is told so. A good book is never overwritten with nothing. Pinned by `test_a_shrunken_book_does_not_overwrite_the_snapshot` and `test_zero_rows_is_unreadable_not_an_empty_book` (Task 2).
2. **The serial moved** (Wireless debugging takes a new port after a reboot). Expected: rediscover and sync, never "locked down". This is already handled by `ready()`'s `_still_attached`. Pinned by `test_query_after_a_port_change_rediscovers` (Task 1).
3. **A sync during a call, or two syncs at once** (voice plus loop). Expected: the second is refused as `BUSY` and the call is untouched. `query` never pushes or runs the helper. Pinned by `test_a_second_sync_while_one_runs_is_busy` (Task 2) and `test_query_never_pushes_or_runs_the_helper` (Task 1).
4. **A corrupt or half-written snapshot** (crash mid-write, hand edit). Expected: the WhatsApp book falls back to the VCF with a warning, the boot is unaffected and the next sync is due at once. Pinned by `test_a_corrupt_snapshot_falls_back_to_the_vcf` (Task 3) and `test_a_corrupt_snapshot_makes_a_sync_due` (Task 2).
5. **A pending read-back across a reload.** He asked to message "Ana", got the read-back, and the weekly sync swapped the book before he said "go". Expected: the confirmation still works for the same number. The token is keyed on contact key and phone, not on list identity. Pinned by `test_a_reload_between_read_back_and_go_keeps_the_confirmation` (Task 3).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `services/phone_adb.py` | modify | `parse_content_rows()`, `AdbPhone.query()`, `ready(need_helper=)`, `AdbPhone.from_config()` |
| `services/contact_sync.py` | create | rows to `Contact`, snapshot read/write, diff, `ContactSync` (status, lock, due) |
| `services/whatsapp_service.py` | modify | snapshot-first `_load_contacts`, `contacts_source` |
| `services/phone_service.py` | modify | `_get_dialer` builds through `AdbPhone.from_config` |
| `core/orchestrator.py` | modify | build `ContactSync`, `_contacts_sync_loop`, `_dispatch_phone(..., contact_sync=None)` |
| `services/llm.py` | modify | `phone_sync_contacts` enum value plus one sentence |
| `config.yaml`, `.gitignore`, `tests/config_fixture.py` | modify | `contacts_sync:` block, ignore the snapshot, fixture path override |
| `CLAUDE.md` | modify | new subsection plus tree/tests entries |
| `tests/test_phone_adb.py`, `tests/test_contact_sync.py` (new), `tests/test_whatsapp_service.py`, `tests/test_orchestrator_phone.py` | tests | |

---

### Task 1: `AdbPhone.query` and a row parser that survives free text

**Files:**
- Modify: `services/phone_adb.py`
- Modify: `services/phone_service.py` (`_get_dialer` only)
- Test: `tests/test_phone_adb.py` (new classes `ContentRowTests`, `QueryTests`, `FromConfigTests`)

**Interfaces:**
- Produces:
  - `parse_content_rows(text: str, keys: list[str]) -> tuple[list[dict[str, str | None]], int]`. Returns `(rows, unparsed_line_count)`. The value `NULL` becomes `None`.
  - `AdbPhone.query(uri: str, projection: list[str], timeout: float = 30.0) -> list[dict[str, str | None]] | None`. `None` means the phone could not be read; `[]` means the provider answered "No result found.". After a call, `self.last_status` holds the `ready()` status (`OK`, `NO_PHONE`, `LOCKED_DOWN`, or `UNREADABLE` when the output had neither rows nor the no-result line).
  - `AdbPhone.ready(need_interception: bool = True, need_helper: bool = True) -> str`. `need_helper=False` skips `_push_helper()`. Existing callers are unchanged.
  - `AdbPhone.from_config(config: dict, **kw) -> AdbPhone`. Reads `phone.adb.path|serial|port|mute_phone_mic` with the same defaults `PhoneService.__init__` uses today. `**kw` passes through (`runner=`, `popen=`, ...).

**Parser algorithm** (the one thing the signature does not determine):
- A row starts at a line matching `^Row: \d+ ` followed by `keys[0]=`.
- For `i < len(keys)-1`, the value of `keys[i]` runs up to the **first** `", " + keys[i+1] + "="` after it.
- The last key takes the rest of the line.
- A line that does not start with `Row: ` is a continuation: it is appended (with `"\n"`) to the previous row's last value. Before any row exists, it is ignored.
- A `Row:` line missing any expected separator counts toward `unparsed` and is dropped.
- Strip a trailing `\r` from every line.
- Docstring rule: **at most one free-text column, and it goes last.**

- [ ] **Step 1: Write the failing tests**

```python
class ContentRowTests(unittest.TestCase):
    KEYS = ["contact_id", "data2", "is_super_primary", "account_type", "data1", "display_name"]

    def test_rows_parse_with_crlf_and_null(self):
        text = ("Row: 0 contact_id=7, data2=2, is_super_primary=0, account_type=com.google, "
                "data1=+351 912 000 001, display_name=Ana Exemplo\r\n"
                "Row: 1 contact_id=8, data2=NULL, is_super_primary=1, account_type=NULL, "
                "data1=912000002, display_name=Rui 🫰🏽\r\n")
        rows, bad = parse_content_rows(text, self.KEYS)
        self.assertEqual(bad, 0)
        self.assertEqual(rows[0]["display_name"], "Ana Exemplo")
        self.assertIsNone(rows[1]["data2"])
        self.assertEqual(rows[1]["display_name"], "Rui 🫰🏽")

    def test_a_name_with_commas_and_equals_is_kept_whole(self):
        text = "Row: 0 contact_id=1, data2=2, is_super_primary=0, account_type=x, data1=1, display_name=Silva, Ana = chefe, data1=9\n"
        rows, _ = parse_content_rows(text, self.KEYS)
        self.assertEqual(rows[0]["display_name"], "Silva, Ana = chefe, data1=9")

    def test_a_number_with_a_pause_comma_is_kept_whole(self):
        text = "Row: 0 contact_id=1, data2=2, is_super_primary=0, account_type=x, data1=212000000,,12, display_name=Loja\n"
        rows, _ = parse_content_rows(text, self.KEYS)
        self.assertEqual(rows[0]["data1"], "212000000,,12")

    def test_a_newline_in_the_last_value_continues_the_row(self):
        text = "Row: 0 contact_id=1, data2=2, is_super_primary=0, account_type=x, data1=1, display_name=Ana\nda Praia\n"
        rows, _ = parse_content_rows(text, self.KEYS)
        self.assertEqual(rows[0]["display_name"], "Ana\nda Praia")

    def test_a_malformed_row_is_counted_not_raised(self):
        rows, bad = parse_content_rows("Row: 0 contact_id=1, display_name=x\n", self.KEYS)
        self.assertEqual((rows, bad), ([], 1))


class QueryTests(_Base):
    # FakeAdb gains: self.content = "<text>" answered for any shell cmd starting "content query"
    def test_query_returns_rows(self):  # content = two rows -> len(result) == 2, last_status "OK"
    def test_no_result_is_an_empty_list(self):  # content = "No result found.\n" -> []
    def test_provider_error_is_none(self):  # content = "Error while accessing provider:com.android.contacts\n..." -> None, last_status "UNREADABLE"
    def test_no_phone_is_none(self):  # devices without the phone -> None, last_status "NO_PHONE", no "content" shell cmd sent
    def test_locked_down_is_none(self):  # gate "0" -> None, last_status "LOCKED_DOWN"
    def test_query_never_pushes_or_runs_the_helper(self):
        # no argv contains "push", no shell cmd contains "CallAudio" or "sha256sum"
    def test_query_is_read_only(self):
        # every shell cmd sent starts with "getprop" or "content query"; none contains insert/update/delete
    def test_query_after_a_port_change_rediscovers(self):
        # serial cached as old port, devices lists new port -> rows returned, adb.calls use the new serial

class FromConfigTests(unittest.TestCase):
    def test_from_config_reads_phone_adb(self):  # config_for_tests() -> adb_path == config["phone"]["adb"]["path"], port == 27183
    def test_from_config_runs_nothing(self):  # runner that raises -> construction succeeds
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONIOENCODING=utf-8 uv run python -m unittest tests.test_phone_adb -v`
Expected: FAIL with `ImportError: cannot import name 'parse_content_rows'`.

- [ ] **Step 3: Implement** `parse_content_rows`, `ready(..., need_helper=True)`, `query` and `from_config` in `services/phone_adb.py`.
  - `query` calls `ready(need_interception=False, need_helper=False)`. It sends exactly one shell command: `content query --uri <uri> --projection <":".join(projection)>`, with `timeout`. The URI and keys are compared against `^[\w:/.]+$` and `^\w+$`; a mismatch raises `ValueError`, so no caller can inject shell.
  - Log `unparsed` at WARNING as a count only.
  - Switch `PhoneService._get_dialer` to `AdbPhone.from_config(self._config)`. If `PhoneService` keeps no `_config`, pass the same values it reads today. `test_phone_service` must stay green unchanged.

- [ ] **Step 4: Run them to see them pass**

Run: `PYTHONIOENCODING=utf-8 uv run python -m unittest tests.test_phone_adb tests.test_phone_service -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/phone_adb.py services/phone_service.py tests/test_phone_adb.py
git commit -m "feat(phone): read-only content query on AdbPhone with a free-text-safe row parser"
```

---

### Task 2: `ContactSync`: rows to contacts, snapshot, diff, status

**Files:**
- Create: `services/contact_sync.py`
- Test: `tests/test_contact_sync.py`

**Interfaces:**
- Consumes: `AdbPhone.query`, `AdbPhone.last_status` and `AdbPhone.from_config` (Task 1); `Contact` and `normalize_phone` from `services/whatsapp_service.py`.
- Produces:
  - `URI = "content://com.android.contacts/data/phones"`
  - `PROJECTION = ["contact_id", "data2", "is_super_primary", "account_type", "data1", "display_name"]` (display_name last, see Task 1)
  - `contacts_from_rows(rows: list[dict], default_cc: str) -> list[Contact]`
  - `load_snapshot(path: Path) -> tuple[list[Contact], datetime | None]`. Missing or corrupt gives `([], None)`; corrupt also logs a WARNING.
  - `write_snapshot(path: Path, contacts: list[Contact], synced_at: datetime) -> None`. Writes `<path>.tmp`, then `os.replace`.
  - `diff_counts(old: list[Contact], new: list[Contact]) -> tuple[int, int, int]`, returning `(added, removed, changed)`.
  - `@dataclass ContactSyncResult(status: str, count: int = 0, added: int = 0, removed: int = 0, changed: int = 0, previous: int = 0, message: str = "")` with `__bool__` = `status in ("SYNCED", "UNCHANGED")`.
  - `class ContactSync` with `__init__(self, config: dict, phone=None, on_synced: Callable[[], object] | None = None, now: Callable[[], datetime] = <aware local now>)`, plus:
    - `.enabled: bool`
    - `.snapshot_path: Path`
    - `.interval_days: int`
    - `.check_interval_minutes: int`
    - `.age_s() -> float | None`
    - `.due() -> bool`
    - `.sync() -> ContactSyncResult`

**Rules `contacts_from_rows` must follow:**
- Group by `contact_id`, keeping first-seen order. The name is the group's first non-empty `display_name`.
- Cut `data1` at the first `,` or `;` (pause/wait), then `normalize_phone(data1, default_cc)`. Drop `None`.
- Rank each number as 3 for `is_super_primary == "1"`, plus 2 for `data2 == "2"` (mobile), plus 1 for `data2 == "1"` (home). This mirrors `_tel_priority`. Sort descending with a stable sort and deduplicate. This collapses the copies under the WhatsApp and Duo accounts.
- Drop a group with no name or no numbers. Two `contact_id`s with the same name stay **two** contacts, because the ambiguity question depends on it.

**Snapshot shape** (`version` 1):

```json
{"version": 1, "synced_at": "2026-10-13T18:02:11+01:00", "source": "adb",
 "contacts": [{"name": "Ana Exemplo", "phones": ["+351912000001"]}]}
```

**`diff_counts`:** key each contact by `name.casefold()`. The value is a sorted list of phone tuples, so duplicate names form a multiset. `added` and `removed` count keys present on only one side. `changed` counts shared keys whose value differs.

**`sync()` sequence:**
1. `enabled` false gives `DISABLED`.
2. Take `self._lock` non-blocking. If that fails, return `BUSY`.
3. `rows = phone.query(URI, PROJECTION)`.
4. `None` maps `last_status` to `NO_PHONE`, `LOCKED_DOWN` or `UNREADABLE`.
5. `[]` or zero contacts after folding gives `UNREADABLE`. A phone with a book never has zero.
6. Load the previous snapshot. If `previous > 0` and `count < previous * min_keep_ratio`, return `SHRUNK` and do not write.
7. Otherwise write (always, so `synced_at` moves) and call `on_synced()`, guarded so its exception is logged, not raised.
8. Return `SYNCED`, or `UNCHANGED` when all three diff counts are 0.
9. Log one INFO line: `Contacts synced from phone: %d contacts (+%d -%d ~%d) in %.1fs`, counts only.

**`due()`:** `enabled and (age_s() is None or age_s() >= interval_days * 86400)`.

**`enabled`:**
- `config["contacts_sync"]["enabled"] is True`
- and `sys.platform == "win32"`
- and adb is found (`shutil.which(path) or os.path.isfile(path)`, the same check as `PhoneService`)
- and `whatsapp.enabled or phone.enabled`, because otherwise nothing reads the snapshot.

Each refusal logs its reason once at INFO. Construction runs no adb. `phone` defaults lazily to `AdbPhone.from_config(config)` on the first `sync()`.

**Spoken messages** (`result.message`, exact copy):

| status | message |
|---|---|
| SYNCED | `"Contacts refreshed from your phone: {count}, {added} new, {removed} gone, {changed} changed."` |
| UNCHANGED | `"Your contacts were already up to date: {count} from your phone."` |
| NO_PHONE | `"I can't reach your phone right now, so I'm keeping the contacts from {age}."` (or `"...keeping the contact book I have."` when there is no snapshot) |
| LOCKED_DOWN | `"The phone has USB debugging (Security settings) switched off, so I can't read its contacts."` |
| UNREADABLE | `"Your phone wouldn't hand over its contacts just now. I kept the ones I had."` |
| SHRUNK | `"Your phone only gave me {count} contacts against {previous} last time, so I kept the old list."` |
| BUSY | `"I'm already refreshing your contacts."` |
| DISABLED | `"Contact sync isn't set up."` |

`{age}` is spoken as `"today"`, `"yesterday"` or `"{n} days ago"`, from `age_s()`.

- [ ] **Step 1: Write the failing tests** (`tests/test_contact_sync.py`)

Build the config with `config_for_tests(contacts_sync={"enabled": True, "snapshot_path": <tmp>/phone_contacts.json})`. Patch `sys.platform` to `"win32"` and `shutil.which` to truthy. The phone is a `Mock` with `query` and `last_status`.

```python
def test_rows_fold_per_contact_and_drop_account_duplicates(self):
    rows = [row(7, "Ana Exemplo", "+351 912 000 001", "2", acct="com.google"),
            row(7, "Ana Exemplo", "912000001", "2", acct="com.whatsapp"),
            row(7, "Ana Exemplo", "212000003", "1")]
    [c] = contacts_from_rows(rows, "351")
    self.assertEqual(c.phones, ["+351912000001", "+351212000003"])

def test_super_primary_number_comes_first(self): ...           # phones[0] is the is_super_primary one even when it is home
def test_pause_suffix_is_cut_before_normalising(self): ...      # "212000000,,12" -> "+351212000000"
def test_same_name_two_ids_stays_two_contacts(self): ...        # len == 2
def test_nameless_or_numberless_groups_are_dropped(self): ...   # display_name None / data1 "111" -> []

def test_snapshot_round_trips(self): ...                        # write then load -> same names/phones, synced_at equal
def test_write_is_atomic(self): ...                             # os.replace patched to raise -> original file bytes unchanged
def test_a_corrupt_snapshot_makes_a_sync_due(self): ...         # file "{" -> load_snapshot == ([], None), due() True

def test_diff_counts(self): ...                                 # old {A,B,C}, new {A, B', D} -> (1, 1, 1)

def test_first_sync_writes_and_reports(self):
    # query -> 3 contacts; result.status "SYNCED", count 3, added 3; file exists; on_synced called once
def test_unchanged_still_moves_synced_at(self): ...             # second sync same rows -> "UNCHANGED", synced_at later
def test_zero_rows_is_unreadable_not_an_empty_book(self): ...   # query -> [] ; status "UNREADABLE", snapshot untouched
def test_a_shrunken_book_does_not_overwrite_the_snapshot(self): # previous 400, now 150 -> "SHRUNK", file bytes unchanged, on_synced not called
def test_no_phone_keeps_the_book_and_names_its_age(self): ...   # query None, last_status "NO_PHONE", snapshot 3 days old -> message contains "3 days ago"
def test_locked_down_has_its_own_line(self): ...
def test_a_second_sync_while_one_runs_is_busy(self):
    # query blocks on an Event; second thread's sync() -> "BUSY" immediately
def test_on_synced_raising_does_not_fail_the_sync(self): ...    # status still "SYNCED"
def test_due_after_seven_days(self): ...                        # now = synced_at + 6d23h -> False; +7d -> True
def test_disabled_off_windows_and_without_a_consumer(self): ... # three cases, each .enabled False, sync() "DISABLED", query never called
def test_construction_runs_no_adb(self): ...                    # AdbPhone.from_config patched to raise -> construct OK
def test_logs_and_messages_carry_no_names_or_numbers(self):
    # assertLogs over a SYNCED run: no record contains "Ana" or "912000001"; neither does result.message
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONIOENCODING=utf-8 uv run python -m unittest tests.test_contact_sync -v`
Expected: FAIL with `ModuleNotFoundError: services.contact_sync`.

- [ ] **Step 3: Implement `services/contact_sync.py`** to the interfaces and rules above. Use `min_keep_ratio` from config (default `0.5`), and `interval_days` (default `7`, floored at 1).

- [ ] **Step 4: Run them to see them pass**

Run: `PYTHONIOENCODING=utf-8 uv run python -m unittest tests.test_contact_sync -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/contact_sync.py tests/test_contact_sync.py
git commit -m "feat(contacts): weekly phone contact snapshot over ADB, counts-only logging"
```

---

### Task 3: WhatsAppService reads the snapshot first, plus config

**Files:**
- Modify: `services/whatsapp_service.py` (`__init__`, `_load_contacts`, `reload_contacts`)
- Modify: `config.yaml`, `.gitignore`, `tests/config_fixture.py`
- Test: `tests/test_whatsapp_service.py` (new class `SnapshotBookTests`)

**Interfaces:**
- Consumes: `load_snapshot(path) -> tuple[list[Contact], datetime | None]` (Task 2).
- Produces:
  - `WhatsAppService.contacts_source: str`, one of `"phone"`, `"vcf"` or `""`.
  - `WhatsAppService.snapshot_path: Path | None`. This is `None` unless `contacts_sync.enabled is True`.
  - `reload_contacts() -> int` keeps its signature and now re-reads the snapshot, then the VCF.

**Rules:**
- A snapshot with at least one contact wins. Otherwise use the VCF, as today.
- Import `load_snapshot` lazily inside `_load_contacts` to avoid an import cycle, since `contact_sync` imports `Contact` from this module.
- Swap `self.contacts` by assignment, never by in-place mutation, so a `resolve_contact` running on another thread sees either the whole old list or the whole new one.
- The INFO line at init names the source and the count only.

**Config** (`config.yaml`, top level, after `whatsapp:`):

```yaml
# The phone's own contact book, read over ADB (phone.adb) into a local
# snapshot that WhatsApp and phone calls resolve names against. Read-only on
# the phone; contacts.vcf stays the fallback. Gitignored: real people's numbers.
contacts_sync:
  enabled: true
  snapshot_path: "phone_contacts.json"
  interval_days: 7              # boot syncs when older than this, then weekly
  check_interval_minutes: 360   # how often the loop asks "is it due"; a missed phone retries here
  min_keep_ratio: 0.5           # a book shrunk below half the last one is not written
```

**`.gitignore`:** `phone_contacts.json` and `phone_contacts.json.tmp`, with a comment matching the `contacts.vcf` one.

**Fixture** (`tests/config_fixture.py`, a path override with a comment): `config.setdefault("contacts_sync", {})["snapshot_path"] = "/nonexistent/phone_contacts.json"`.

- [ ] **Step 1: Write the failing tests**

```python
class SnapshotBookTests(unittest.TestCase):
    def test_the_snapshot_beats_the_vcf(self):
        # tmp snapshot with "Ana Exemplo"; VCF with "Rui Vcf" -> contacts names == ["Ana Exemplo"], contacts_source "phone"
    def test_an_empty_or_missing_snapshot_falls_back_to_the_vcf(self): ...  # contacts_source "vcf"
    def test_a_corrupt_snapshot_falls_back_to_the_vcf(self): ...            # file "{" -> VCF contacts, no raise
    def test_snapshot_ignored_when_sync_disabled(self): ...                 # contacts_sync.enabled False -> VCF, snapshot_path None
    def test_reload_picks_up_a_new_snapshot(self): ...                      # rewrite file, reload_contacts() == new count
    def test_a_reload_between_read_back_and_go_keeps_the_confirmation(self):
        # loose match -> send() returns read-back; reload_contacts() with same contact; send(confirm=True) sends once
    def test_the_fixture_never_reads_the_real_snapshot(self):
        self.assertEqual(config_for_tests()["contacts_sync"]["snapshot_path"], "/nonexistent/phone_contacts.json")
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONIOENCODING=utf-8 uv run python -m unittest tests.test_whatsapp_service -v`
Expected: FAIL with `AttributeError: contacts_source`.

- [ ] **Step 3: Implement** the loader change and the config, `.gitignore` and fixture edits.

- [ ] **Step 4: Run the whole suite** (this is the step where the fixture changes)

Run: `PYTHONIOENCODING=utf-8 uv run python -m unittest discover -s tests`
Expected: PASS, including `test_config_fixture` and `test_llm_prompt`'s roster guard.

- [ ] **Step 5: Commit**

```bash
git add services/whatsapp_service.py config.yaml .gitignore tests/config_fixture.py tests/test_whatsapp_service.py
git commit -m "feat(whatsapp): resolve names against the phone contact snapshot, VCF as fallback"
```

---

### Task 4: Boot check, weekly loop and "refresh my contacts"

**Files:**
- Modify: `core/orchestrator.py` (construction next to `PhoneService(...)`, the loop next to `_whatsapp_groups_loop`, `_dispatch_phone`, `_handle_tool_call`'s phone branch)
- Modify: `services/llm.py` (`control_phone` enum and description)
- Test: `tests/test_orchestrator_phone.py`, `tests/test_llm_prompt.py`

**Interfaces:**
- Consumes: `ContactSync(config, on_synced=self.whatsapp_service.reload_contacts)`, `.enabled`, `.due()`, `.sync()`, `.check_interval_minutes` (Task 2).
- Produces:
  - `Orchestrator.contact_sync: ContactSync`
  - `Orchestrator._contacts_sync_loop(self, interval_minutes: int) -> None`
  - `_dispatch_phone(params: dict, phone_svc, contact_sync=None) -> str`, a trailing defaulted parameter so the existing two-argument call sites keep working.
  - The control_phone enum value `"phone_sync_contacts"`.

**Loop semantics:**
- Start it only when `contact_sync.enabled is True`, on a daemon thread named `"contacts-sync"`.
- Unlike the other loops it checks **first**, then waits: `while True: if due(): sync(); if _background_stop.wait(max(60, minutes*60)): return`. The boot sync therefore happens without delaying `__init__`, a missed phone retries every `check_interval_minutes`, and a fresh snapshot costs only a file stat.
- Exceptions are logged and the loop continues, the same as `_whatsapp_groups_loop`.

**Dispatch:**
- `phone_sync_contacts` is handled **before** the `phone_svc.enabled` gate. Contact sync does not need Gemini credentials, and "Phone calls aren't set up" would be the wrong answer.
- It returns `contact_sync.sync().message`.
- With `contact_sync` `None`, it returns `"Contact sync isn't set up."`
- A success maps to `_report(True)`, and NO_PHONE, UNREADABLE and SHRUNK map to `_report(False)`. BUSY and DISABLED report nothing (the mood rules).

**LLM schema:**
- Add `"phone_sync_contacts"` to the `control_phone` `action` enum.
- Append exactly this sentence to the description: `"phone_sync_contacts re-reads his phone's contact book now (refresh my contacts); it is also done weekly by itself."`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orchestrator_phone.py
def test_sync_contacts_speaks_the_service_line(self):
    sync = Mock(); sync.sync.return_value = ContactSyncResult("SYNCED", 3, 1, 0, 0, message="Contacts refreshed from your phone: 3, 1 new, 0 gone, 0 changed.")
    self.assertEqual(_dispatch_phone({"action": "phone_sync_contacts"}, Mock(enabled=True), sync), sync.sync.return_value.message)
def test_sync_contacts_works_with_calls_disabled(self): ...      # phone_svc Mock(enabled=False) -> still calls sync.sync()
def test_sync_contacts_without_a_syncer(self): ...               # -> "Contact sync isn't set up."
def test_old_two_argument_calls_still_work(self): ...            # _dispatch_phone({"action": "phone_find"}, service) unchanged

class ContactsLoopTests(unittest.TestCase):   # Orchestrator.__new__, bare attributes
    def test_due_at_boot_syncs_before_the_first_wait(self): ...  # _background_stop.wait returns True; sync called once
    def test_fresh_snapshot_does_not_sync(self): ...             # due() False -> sync never called
    def test_a_raising_sync_does_not_end_the_loop(self): ...     # sync raises, wait returns False then True -> due() asked twice

# tests/test_llm_prompt.py
def test_control_phone_offers_contact_sync(self): ...            # "phone_sync_contacts" in the enum
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONIOENCODING=utf-8 uv run python -m unittest tests.test_orchestrator_phone tests.test_llm_prompt -v`
Expected: FAIL.

- [ ] **Step 3: Implement** the construction, the loop, the dispatch branch and the schema line. Construct `ContactSync` after `whatsapp_service` exists. It must stay construction-only, with no adb in `__init__`.

- [ ] **Step 4: Run the whole suite**

Run: `PYTHONIOENCODING=utf-8 uv run python -m unittest discover -s tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/orchestrator.py services/llm.py tests/test_orchestrator_phone.py tests/test_llm_prompt.py
git commit -m "feat(contacts): sync at boot when stale, weekly, and on 'refresh my contacts'"
```

---

### Task 5: Live check and CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`. Add a new `### Contact sync` subsection after "WhatsAppService". Add `services/contact_sync.py`, `tests/test_contact_sync.py` and `phone_contacts.json` to the Project Structure tree. Add `phone_sync_contacts` to the `control_phone` action list (and its count). Add a bullet to the coverage list under "Testing and Validation".

- [ ] **Step 1: Live read-only run** (the phone on Wireless debugging):

```bash
PYTHONIOENCODING=utf-8 uv run python -c "import yaml; from services.contact_sync import ContactSync; c=yaml.safe_load(open('config.yaml',encoding='utf-8')); r=ContactSync(c).sync(); print(r.status, r.count, r.added, r.removed, r.changed)"
```

Expected: `SYNCED` with a count near 418 (fewer once numberless and short-code cards are dropped). A second run prints `UNCHANGED`. Check by count only that `phone_contacts.json` exists and that `git status` does not list it.

- [ ] **Step 2: Resolution parity.** Resolve three names he uses, through `WhatsAppService(c).resolve_contact` with `contacts_source == "phone"`. Each must give the same certainty as with the VCF. Compare `certain` and `bool(phone)`, and do not print the numbers.

- [ ] **Step 3: Write the CLAUDE.md subsection.** Cover:
  - the probe findings (845 rows, 418 ids, account duplicates, `display_name` last and why);
  - shell versus helper;
  - snapshot-first loading;
  - SHRUNK and why;
  - counts-only logging;
  - the loop's check-first order;
  - `phone_sync_contacts` being handled before the phone gate.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: contact sync from the phone"
```

---

## Shared touch points

For reconciling this plan with the "missed calls + notifications digest" and "incoming-call announcement" plans:

- **`services/phone_adb.py`**
  - **new** `parse_content_rows(text, keys) -> (rows, unparsed)`. This is a generic `content query` parser that the missed-calls plan can reuse for `content://call_log/calls`. Rule: the free-text column goes last, so for the call log put `name` last.
  - **new** `AdbPhone.query(uri, projection, timeout=30.0) -> list[dict] | None` and the attribute `AdbPhone.last_status`.
  - **changed** `AdbPhone.ready(need_interception=True, need_helper=True)`. The new kwarg skips the helper push for read-only work. Existing callers are unaffected.
  - **new** classmethod `AdbPhone.from_config(config, **kw)`, the one place that reads `phone.adb.*`.
  - No lock is added to `AdbPhone`. `query` is stateless apart from `ready()`'s serial rediscovery. If a sibling adds an `AdbPhone`-wide lock, `query` should take it.
- **`services/phone_service.py`:** `_get_dialer` builds through `AdbPhone.from_config`. No behaviour change.
- **`core/orchestrator.py`**
  - **new** `self.contact_sync` and **new** `_contacts_sync_loop` (thread `"contacts-sync"`, stopped by `_background_stop`, check-first-then-wait).
  - **changed** `_dispatch_phone(params, phone_svc, contact_sync=None)`, with a trailing defaulted parameter. A new action branch sits **before** the `phone_svc.enabled` gate. Siblings that add actions not needing Gemini (missed calls, notifications) should follow the same pattern and add their own trailing parameters after `contact_sync`.
- **`services/llm.py`:** the `control_phone` enum gains `phone_sync_contacts` plus one description sentence. Siblings appending actions edit the same enum list and description string, so merge by hand.
- **`services/whatsapp_service.py`:** `_load_contacts` is snapshot-first, with new attributes `contacts_source` and `snapshot_path`. The missed-calls plan's "names from the phone's own contacts" can resolve numbers against `whatsapp_service.contacts`, which is now the phone's book. No reverse-lookup method is added here.
- **`config.yaml`:** new top-level `contacts_sync:` block. `phone.adb` is unchanged.
- **`tests/config_fixture.py`:** new path override `contacts_sync.snapshot_path`.
- **`tests/test_phone_adb.py`:** `FakeAdb` gains a `content` attribute answered for shell commands starting `content query`. A sibling extending `FakeAdb` for `dumpsys notification` or the call log should key on the command prefix the same way.
- **`.gitignore`:** `phone_contacts.json`, `phone_contacts.json.tmp`.
