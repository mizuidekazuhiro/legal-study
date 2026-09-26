# Windows logon watcher

`scripts\Start-LegalStudyWatcher.ps1 -Config <absolute config.json>` runs
`python -m legal_study.automation.watch_service --config <absolute config.json>` and
supervises the existing `watch-study` and `watch-chat-bridge` commands. Use the
repository's existing `.venv\Scripts\python.exe`, with the repository as the
Task Scheduler working directory. The task is `LegalStudy-OCR-Watcher`, triggered
once at user logon, with IgnoreNew, unlimited runtime, and three failure retries
one minute apart. The launcher also retries nonzero service exits up to three
times because Scheduler retry alone did not recover a forced exit in local
validation. Successful exits do not restart. No administrator privileges
are needed by the watcher.

Configuration keys: repository, home, service_directory, pdf, subject,
bridge_root, obsidian_inbox, and optional poll_seconds (default 30).
Keep production state in `~/.legal-study/service`. Google Drive may mount after
logon: the service waits without opening PDFs. On first installation it records
only the PDF size and modification time, then starts PDF processing after a new
change. Previously activated processing resumes on later service starts using
the existing pipeline's persistent recovery. The initial configuration does not
reprocess old PDFs. Bridge commands retain their existing explicit-approval rules;
Notion integration is not enabled by this service.

The service and both CLI watcher commands hold OS file locks. Lock files are
small and persistent, but locks are released by Windows on normal or abnormal
exit. A Windows Job object owns the children, preventing orphan watchers when
Task Scheduler ends the service. `--stop` requests a normal service exit; it does
not change task settings or delete learning data. Only owned child processes stop.

Each of supervisor, study and bridge logs is capped at 2 MiB plus three backups.
The heartbeat is a small atomic JSON file. `Test-LegalStudyHealth.ps1` reads task,
heartbeat and process metadata, at most 100 recent lines per log, and volume free
space. The daily SYSTEM audit never executes user-writable Python or re-OCRs PDFs.
Volume free-space changes are a coarse storage-growth signal, not an OCR-specific
size measurement. Health reports live alongside the existing DailyProgramAudit
reports.
