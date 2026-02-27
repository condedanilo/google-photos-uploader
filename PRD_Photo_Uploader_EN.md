# PRD — Google Photos Uploader

**Version:** 0.4  
**Date:** February 2026  
**Status:** Draft for review

---

## 1. Overview

A command-line tool (CLI) for bulk uploading photos from an external hard drive to Google Photos, with support for resumable execution, progress reporting, and basic organization.

The tool is for personal use, running locally on the user's machine, with no need for hosting or external exposure.

---

## 2. Problem

The user has thousands of photos stored on an external hard drive and wants to migrate them to Google Photos. Current alternatives fall short because:

- The volume of files is large, and any interruption (power outage, network error, etc.) can cause loss of progress
- There is no clear visibility into how many photos have been processed, how many have failed, and how much time is left
- Generic tools do not respect the existing folder organization structure

---

## 3. Goals

Build a CLI tool that:

- Reliably uploads photos from a local directory to Google Photos
- Persists progress state locally, allowing resumption of an interrupted run
- Provides clear real-time progress reporting and a final summary at the end of each run
- Automatically retries on transient failures
- Uploads photos in compressed quality to save storage space in Google Photos
- Delivers a clear and friendly user experience even as a terminal tool
- Is portable — runs on any operating system

---

## 4. Technical Feasibility — Google Photos API

> ✅ **Project is viable.** Investigation conducted in February 2026.

Changes made by Google to the API in March 2025 only affected **reading** third-party libraries. **Photo uploads remain fully supported** via the `photoslibrary.appendonly` scope, with no restrictions.

The upload process works in two steps:
1. Upload the file bytes to obtain an upload token
2. Call `mediaItems.batchCreate` with the token to register the item in the user's library

**Relevant API constraints:**
- Maximum of 50 items per `batchCreate` call
- Only one active `batchCreate` call at a time per user (bytes can be uploaded in parallel)
- 200MB file size limit per image
- The API does not offer native quality/compression control — **compression must be done locally before upload**

---

## 5. Out of Scope — v1

The items below are recognized as valuable but are intentionally excluded from the first version:

- Detection and handling of duplicate photos
- Mapping local folders to Google Photos albums
- Image analysis with an LLM for automatic tag and description generation
- Concurrent execution control via file lock
- `--dry-run` mode
- `--status` command for external process querying
- ASCII dashboard with real-time progress table

These topics are documented in the Roadmap section (section 12).

---

## 6. Target User

Single user (the developer themselves), with enough technical knowledge to run a terminal tool and configure API credentials. No graphical interface or simplified onboarding is required — but the experience should be clear, human, and comfortable for long-running processes.

---

## 7. Functional Requirements

### 7.1 Photo Upload

- The tool must accept a local directory path as input
- It must recursively scan the directory and identify all supported media files (JPG, PNG, HEIC, MP4, MOV, and other formats accepted by Google Photos)
- It must upload each file to Google Photos via the official API (scope `photoslibrary.appendonly`)
- Files already successfully uploaded in previous runs must be identified by their **content hash** (not by name or path), ensuring that moved or renamed files are not uploaded again
- During the scan and hash calculation phase, continuous visual feedback must be shown ("Scanning files... 1,243 found so far") so the process does not appear frozen

### 7.2 Image Compression

- Photos must be compressed locally before upload to save storage space in Google Photos
- Compression should be equivalent to Google Photos' "Storage saver" quality mode
- Compression behavior must be configurable (enable/disable)

### 7.3 State Persistence

- Progress state must be saved locally to a file on disk
- For each file, the state must record: path, content hash, status (pending / in progress / uploaded / error), upload timestamp, number of attempts made, and error message if applicable
- On startup, the tool must detect whether a previous state exists and ask the user whether to continue from where it left off or start over
- Files that were in "in progress" status at the time of an interruption must be automatically reverted to "pending" on the next run
- State must be saved incrementally and safely — resilient to abrupt shutdowns, including system hibernation

### 7.4 Automatic Retry

- On upload failure, the tool must automatically retry with exponential backoff
- The maximum number of retries per file must be configurable (suggested default: 3)
- After exhausting retries, the file must be marked as error and the tool must continue to the next file
- For errors suggesting a more serious problem (e.g. quota exhausted, prolonged connection loss), the tool must pause execution, display a clear message with the error type, and guide the user on how to proceed

### 7.5 Progress Reporting

During execution, the tool must display in real time:

- Total files identified
- Number successfully uploaded
- Number remaining
- Number with errors
- Completion percentage
- Elapsed time and estimated time remaining
- Upload rate (files per minute)

At the end of execution (or when interrupted), it must generate a report with:

- General summary (totals above)
- List of failed files with the reason for each error

### 7.6 Authentication

- The tool must authenticate with the Google Photos API via OAuth 2.0 using the `photoslibrary.appendonly` scope
- The access token must be persisted locally to avoid re-authentication on every run
- It must support automatic token refresh when expired

### 7.7 User Experience (UX)

#### Onboarding — first run
- On the first run, the tool must guide the user step by step through the OAuth authentication process: explain what will happen, open the browser automatically, and confirm when authentication is successfully completed

#### Summary before starting
- Before beginning any uploads, display a summary of what will happen and wait for user confirmation:
```
Found 3,847 photos.
  → 142 were already uploaded and will be skipped.
  → 3,705 files will be uploaded now.
  → Estimated time: ~2h30min with 4 workers.

Continue? [Y/n]
```

#### Human-readable error messages
- API and system errors must be translated into plain language with actionable guidance. Examples:
  - Instead of `Error 429`: "Request limit reached — waiting 30s before retrying."
  - Instead of `Error 401`: "Access token expired — refreshing automatically..."
  - Instead of `ENOENT`: "File not found — it may have been moved or deleted during the process."

#### Graceful shutdown
- When receiving Ctrl+C, the tool must not terminate abruptly. It must:
  1. Display: "Shutting down safely... please wait."
  2. Finish saving the current state
  3. Display a quick progress summary up to that point
  4. Exit cleanly
- If the user presses Ctrl+C twice in a row, terminate immediately

#### Completion notification
- When the process finishes, emit an OS notification and/or a beep so the user knows it's done even while in another window

---

## 8. Edge Case Handling

### 8.1 File-related errors

| Situation | Expected behavior |
|-----------|-------------------|
| Corrupted or unreadable file | Skip, log as error with description, continue |
| File with 0 bytes | Skip, log as error, continue |
| Image extension but invalid content | Skip, log as error, continue |
| Name with special characters or path too long | Attempt to normalize; if it fails, log as error |
| File larger than 200MB | Skip, log as error with specific message |
| File moved or renamed after process started | Identified via hash; if already uploaded, mark as uploaded and skip |

### 8.2 Execution-related errors

| Situation | Expected behavior |
|-----------|-------------------|
| Ctrl+C or abrupt shutdown | In-progress file reverts to "pending"; saved state is preserved |
| System hibernation during execution | Persisted state is preserved; on resume, "in progress" files revert to "pending" |
| External drive disconnected during execution | Detect read error, pause execution, notify the user |
| Local disk full (unable to save state) | Pause execution and alert the user immediately |
| Simultaneous execution (two terminals) | Not handled in v1 — see Roadmap |

### 8.3 Google Photos API errors

| Situation | Expected behavior |
|-----------|-------------------|
| Transient error (timeout, unstable network) | Automatic retry with exponential backoff |
| Rate limit reached | Wait and retry automatically |
| Daily quota exhausted | Pause execution, display clear message, guide the user |
| Token expired | Automatic refresh; if it fails, prompt re-authentication |
| Unknown persistent error | After N attempts, pause execution and display error details |

### 8.4 State-related errors

| Situation | Expected behavior |
|-----------|-------------------|
| Corrupted state file | Attempt to recover what is possible; unrecoverable items revert to "pending" |
| Missing state file | Start from scratch, as if it were the first run |

---

## 9. Non-Functional Requirements

- **Portability:** must run on macOS, Linux, and Windows without modification
- **Reliability:** must not lose state even in the event of an abrupt shutdown (e.g. Ctrl+C) or system hibernation
- **Performance and Parallelism:** file bytes can be uploaded in parallel with a configurable number of workers (e.g. 4, 6 threads). The `batchCreate` call must be serialized due to API limitations. The system must respect Google Photos rate limits
- **Configurability:** parameters such as number of retries, parallel workers, and compression must be adjustable via configuration file or flags
- **Security:** credentials and tokens must never be logged or exposed; the OAuth scope must be the minimum necessary (`photoslibrary.appendonly`)
- **Modularity:** the upload core must be implemented as an independent library, decoupled from the CLI interface, enabling reuse in future interfaces (web, desktop)
- **Distributability:** technical decisions should account for the possibility of future public distribution — choose a language/runtime with a good packaging story, avoid hard-to-replicate environment dependencies, and always keep credentials separate from code

---

## 10. Main Usage Flow

1. User connects the external drive and runs the tool pointing to the directory
2. If it is the first run: guided OAuth authentication onboarding
3. Tool checks whether a previous state exists and asks whether to continue or restart
4. Tool scans the directory with real-time feedback, computes each file's hash, and builds the upload queue
5. Displays a summary of what will happen and waits for user confirmation
6. Parallel workers upload the (compressed) photo bytes; `batchCreate` is called serially
7. Progress is displayed in real time in the terminal
8. On completion, emits an OS notification and displays the final report with error log (if any)

---

## 11. Open Questions

- What is the estimated number of photos? (helps define concurrency strategy and estimate total time)
- Is there a need to filter by file type or date during the directory scan?
- In the event of a serious persistent error (e.g. quota exhausted), does the user prefer the tool to pause and wait, or to exit and notify?

---

## 12. Version Roadmap

### v1 — Functional core + solid base experience
Everything described in sections 7, 8, and 9 of this document.

### v2 — Productivity and advanced visibility

| Feature | Description |
|---------|-------------|
| `--dry-run` mode | Simulates execution without uploading anything: shows how many files would be sent, skipped, or flagged as problematic |
| `--status` command | Allows querying the progress of a running process from another terminal |
| ASCII dashboard | Replaces the linear log with a real-time progress table: files being uploaded, percentage, time remaining, errors — mini-dashboard style in the terminal |
| Concurrent execution control | File lock to prevent two instances from running simultaneously on the same directory |

### v3 — Organization and intelligence

| Feature | Description |
|---------|-------------|
| Duplicate detection | Checks whether the photo already exists in Google Photos before uploading |
| Album organization | Maps local folder structure to Google Photos albums |
| LLM analysis | Local pipeline with a vision model to generate tags, descriptions, and groupings before upload |
