# MORGAN T. SAMPLE

Riverbend, Ohio | morgan.sample@example.com | 555-0142 | example.com/in/morgansample

---

## Summary

Morgan Sample builds data platforms for regulated industries. Across twelve years the work moved from reporting pipelines to platform architecture.

Recent work centers on streaming ingestion, schema governance, and the tooling that keeps both honest.

---

## Core Skills

**Platforms and Engineering:** Python, Go, PostgreSQL, Kafka, Terraform, Docker, GitHub Actions

**Data and Governance:** dimensional modeling, schema registry design, data lineage, data quality rules, metadata cataloging

---

## Professional Experience

### Fabrikam Data Works

**Staff Platform Engineer**  
(title of record: Staff Platform Engineer II)  
April 2021 to Present | Riverbend, Ohio (Remote)

Worked on platform architecture within a team of six engineers, alongside the analytics and compliance groups on every schema change.

**Streaming and ingestion**

- Rebuilt the ingestion tier on Kafka, cutting median end to end latency from 40 minutes to 90 seconds across 212 source feeds
- Designed the schema registry and its compatibility rules with two other engineers, which stopped 31 breaking changes before release
- Wrote the Terraform modules that provision every environment, replacing a runbook that took two days
  - The modules cover networking, storage, and access policy, with secrets held in Vault

**Governance and quality**

- Contributed to the lineage service that traces any column back to its source system, which four downstream teams later adopted
- Introduced data quality rules covering 1,400 columns, with failures routed to the owning team rather than a shared inbox
- Used an AI coding assistant to draft and review the migration scripts for the registry cutover, keeping the review burden on the team rather than on one person

### Adventure Works Health

**Senior Data Engineer**  
July 2017 to March 2021 | Lakeshore, Ohio

Ran the clinical reporting warehouse and its nightly load, working with analysts and clinicians on definitions.

- Modeled the clinical reporting warehouse in PostgreSQL, reducing the nightly load window from 7 hours to 95 minutes
- Automated the reconciliation process that had consumed two analyst days a week
- Rewrote the load orchestration in Python so a failed step could resume without replaying the whole night

### Tailspin Logistics

**Analytics Engineer**  
September 2013 to June 2017 | Millbrook, Ohio

Built reporting pipelines for a freight brokerage.

- Delivered the first automated reporting pipeline, replacing spreadsheets shared by email

---

## Education

**Bachelor of Science, Computer Science**, Example State University, Millbrook, Ohio, 2009 to 2013

---

## Publications

- Sample M, Roe R. Schema governance at scale. *Journal of Example Systems*. 2023;12(4):220-231.
- Sample M. Lineage without ceremony. *Example Data Quarterly*. 2022;8(2):44-51.
- Sample M, Poe P. Reconciliation as a first class concern. *Example Systems Review*. 2020;5(1):12-19.
