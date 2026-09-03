### Entry 4
| | |
|---|---|
| どう確かめるか *(draft, approved for Wednesday, reframed 2026-09-02)* | Since the rate calculation is Superset's job, verification here scopes to the **fact export**: (a) spot-check `pub`'s repair `from`/`to` against 2-3 real records, including one still-open repair — confirm it exports as null/open, not pre-capped at any boundary on the on-premise side; (b) cross-check the real-failure classification against a sample's raw `event_note`/`failure_reason` text. The period-length math itself (Miyazawa-san's Aug/50% example) is Superset's to verify |
---


### Entry 5
| | |
|---|---|
| Phase2 の定義 *(draft, pending confirmation)* | `COUNT(rental_id)` per `client_device_number × hospital × recipient_department` within the selected period, one count per rental record regardless of month span — no explode-by-month for this metric (that belongs to monthly-trend metrics only). `管理No` maps to `client_device_number`, pending Miyazawa-san's explicit confirmation |
| どう確かめるか *(draft, approved for Wednesday)* | Option A: manual count against raw rental history for 5-10 sample devices, incl. one spanning a month boundary. Option B: total-reconciliation (sum of per-device counts = overall `COUNT(*)` for the period). Recommendation: B first (cheap, catches systemic join/dedup bugs), then A on a handful of devices to confirm per-device correctness |