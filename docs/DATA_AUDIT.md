# Sewa Setu Data Audit

Conducted on: 2026-10-16 against live PostgreSQL instance loaded from `seed/seed.sql`.

---

## 1. Summary of Key Metrics

| Metric | Result | Description / Notes |
|---|---|---|
| **Total Rows in Seed** | **100,000** | Initial snapshot before test runs (100,002 after 2 Phase 1 verification runs). |
| **Empty `doc_path`** | **100,000** | **Exactly 100% of seed rows have empty/null `doc_path`**. Only the 2 test runs have documents. |
| **Age < 60 at `submitted_at`** | **3,083** | Legacy rows admitted where `submitted_at < dob + 60 years`. |
| **Malformed IFSC** | **16,576** | All 16,576 rows have `UTBI0PUR2819` (12 characters instead of standard 11). |
| **Duplicate Bank Accounts across Mobiles** | **0** | No cross-mobile bank account duplicates exist in seed data. |
| **Mobiles with Multiple Active Apps** | **0** | No mobiles with >1 active application exist in seed data. |
| **PENDING older than 15 days** | **0** (post-boot) | All **700** PENDING applications in seed were deemed approved by RTPS scheduler on boot. |
| **Most Common `decided_by`** | **`admin` (89,902)** | Followed by `RTPS-AUTO` (9,700) and `APPLICANT` (400). |

---

## 2. Counts by Status

### Query
```sql
SELECT status, count(*) 
FROM applications 
GROUP BY status 
ORDER BY count(*) DESC;
```

### Result
| Status | Count | Notes |
|---|---|---|
| `APPROVED` | 82,901 | 82,900 from seed + 1 from Phase 1 test approval |
| `DEEMED_APPROVED` | 9,700 | 9,000 from seed + 700 deemed approved on startup by `deemed_approval.py` |
| `REJECTED` | 7,001 | 7,000 from seed + 1 from Phase 1 test rejection |
| `WITHDRAWN` | 400 | All 400 decided by `APPLICANT` |
| `PENDING` | 0 | (0 active pending at audit completion) |
| **Total** | **100,002** | |

---

## 3. Counts by Block

### Query
```sql
SELECT block, count(*) 
FROM applications 
GROUP BY block 
ORDER BY count(*) DESC;
```

### Result
| Block | Total Count |
|---|---|
| `Khelua` | 16,761 |
| `Rajapara` | 16,752 (includes 1 test row) |
| `Borgaon` | 16,715 |
| `Dhemaji Pathar` | 16,655 |
| `Sonari` | 16,597 (includes 1 test row) |
| `Namti` | 16,522 |

Applications are evenly distributed across all 6 statutory blocks (~16.5k each).

---

## 4. Cross-Tabulation: Block by Status

### Query
```sql
SELECT block, status, count(*) 
FROM applications 
GROUP BY block, status 
ORDER BY block, status;
```

### Result
| Block | Status | Count |
|---|---|---|
| Borgaon | APPROVED | 13,903 |
| Borgaon | DEEMED_APPROVED | 1,581 |
| Borgaon | REJECTED | 1,156 |
| Borgaon | WITHDRAWN | 75 |
| Dhemaji Pathar | APPROVED | 13,800 |
| Dhemaji Pathar | DEEMED_APPROVED | 1,616 |
| Dhemaji Pathar | REJECTED | 1,180 |
| Dhemaji Pathar | WITHDRAWN | 59 |
| Khelua | APPROVED | 13,937 |
| Khelua | DEEMED_APPROVED | 1,621 |
| Khelua | REJECTED | 1,138 |
| Khelua | WITHDRAWN | 65 |
| Namti | APPROVED | 13,653 |
| Namti | DEEMED_APPROVED | 1,633 |
| Namti | REJECTED | 1,174 |
| Namti | WITHDRAWN | 62 |
| Rajapara | APPROVED | 13,846 |
| Rajapara | DEEMED_APPROVED | 1,664 |
| Rajapara | REJECTED | 1,166 |
| Rajapara | WITHDRAWN | 76 |
| Sonari | APPROVED | 13,762 |
| Sonari | DEEMED_APPROVED | 1,585 |
| Sonari | REJECTED | 1,187 |
| Sonari | WITHDRAWN | 63 |

---

## 5. Seed Rows with Empty `doc_path`

### Query
```sql
SELECT count(*) 
FROM applications 
WHERE doc_path IS NULL OR doc_path = '';
```

### Result
**100,000** rows.

Verification of non-empty rows:
```sql
SELECT count(*) 
FROM applications 
WHERE doc_path IS NOT NULL AND doc_path != '';
```
Result: **2** rows (both created during Phase 1 interactive verification tests).

**Implication for Assistant Design:**
Every single legacy seed row lacks a document path on file. Therefore, "missing document" must strictly be treated as a **soft flag** (`no_document_on_file`) for officers, rather than a hard block to processing or review.

---

## 6. Under-Age Submissions (`age < 60` at `submitted_at`)

### Query
```sql
SELECT count(*) 
FROM applications 
WHERE submitted_at::date < (dob + interval '60 years');
```

### Result
**3,083** rows.

### Sample Rows
```sql
SELECT id, application_no, dob, submitted_at, EXTRACT(YEAR FROM age(submitted_at::date, dob)) as age_at_sub 
FROM applications 
WHERE submitted_at::date < (dob + interval '60 years') 
LIMIT 5;
```
| ID | Application No | DOB | Submitted At | Age at Submission |
|---|---|---|---|---|
| 26 | SSP26440325 | 1966-05-22 | 2026-05-12 13:59:00 | 59 years |
| 71 | SSP25683589 | 1966-06-04 | 2025-08-25 17:00:00 | 59 years |
| 301 | SSP26177978 | 1966-06-16 | 2026-03-23 12:31:00 | 59 years |
| 317 | SSP26321782 | 1966-09-06 | 2026-07-13 14:04:00 | 59 years |
| 331 | SSP26932603 | 1966-07-19 | 2026-03-25 15:05:00 | 59 years |

**Implication for Assistant Design:**
Historical data contains entries accepted prior to turning 60. For the officer assistant, `age_below_60_at_submission` must be a **hard flag** that blocks approval.

---

## 7. Duplicate `bank_account` Across Different Mobiles

### Query
```sql
SELECT count(*) 
FROM (
    SELECT bank_account 
    FROM applications 
    GROUP BY bank_account 
    HAVING count(DISTINCT mobile) > 1
) s;
```

### Result
**0** distinct bank accounts shared across different mobiles.

**Implication for Assistant Design:**
The seed database does not have bank account reuse. Any bank account reuse across distinct mobile numbers in active applications represents a potential fraud pattern and must trigger the `duplicate_bank_account` flag.

---

## 8. Mobiles with More Than One Active Application

### Query
```sql
SELECT count(*) 
FROM (
    SELECT mobile 
    FROM applications 
    WHERE status IN ('PENDING', 'APPROVED', 'DEEMED_APPROVED') 
    GROUP BY mobile 
    HAVING count(*) > 1
) s;
```

### Result
**0** mobiles with more than one active application.

Checking any mobile with multiple applications across all statuses (including REJECTED and WITHDRAWN):
```sql
SELECT count(*) 
FROM (
    SELECT mobile 
    FROM applications 
    GROUP BY mobile 
    HAVING count(*) > 1
) s;
```
Result: **0** mobiles.

---

## 9. PENDING Older Than 15 Days (SLA Overdue)

### Query
```sql
SELECT count(*) 
FROM applications 
WHERE status = 'PENDING' AND submitted_at < now() - interval '15 days';
```

### Result
**0** currently.

**Context:**
`seed/seed.sql` contained exactly **700** rows with `status = 'PENDING'`. Because the seed snapshot was generated before October 2026, all 700 rows had `submitted_at` older than 15 days. On container startup, the `scheduler` container ran `python /app/scripts/deemed_approval.py`, which marked all 700 rows as `DEEMED_APPROVED` by `RTPS-AUTO` at timestamp `2026-10-16 08:30:29 UTC`.

---

## 10. Malformed IFSC Codes

### Query
```sql
SELECT count(*) 
FROM applications 
WHERE ifsc !~ '^[A-Z]{4}0[A-Z0-9]{6}$';
```

### Result
**16,576** rows.

### Distinct Malformed IFSC Values
```sql
SELECT ifsc, count(*) 
FROM applications 
WHERE ifsc !~ '^[A-Z]{4}0[A-Z0-9]{6}$' 
GROUP BY ifsc;
```
| IFSC | Count | Issue |
|---|---|---|
| `UTBI0PUR2819` | 16,576 | 12 characters long (`PUR2819` has 7 chars instead of 6). Standard IFSC is exactly 11 characters (`^[A-Z]{4}0[A-Z0-9]{6}$`). |

**Implication for Assistant Design:**
`ifsc_malformed` must be raised as a deterministic flag for officer review.

---

## 11. Most Common `decided_by`

### Query
```sql
SELECT decided_by, count(*) 
FROM applications 
GROUP BY decided_by 
ORDER BY count(*) DESC;
```

### Result
| `decided_by` | Count | Description |
|---|---|---|
| `admin` | 89,902 | Departmental officer actions (82,900 approved + 7,000 rejected in seed + 2 test actions) |
| `RTPS-AUTO` | 9,700 | Automated deemed approvals (9,000 in seed + 700 processed on scheduler boot) |
| `APPLICANT` | 400 | Self-service citizen withdrawals |
| `NULL` | 0 | No undecided rows currently |

---

## 12. Verification Checklist
- [x] Concrete numbers obtained directly from running PostgreSQL container.
- [x] Exact count of seed rows with empty `doc_path`: **100,000**.
- [x] SQL queries included verbatim for every metric.
