# Troubleshooting

If harvesting-dune runs into a problem, the companion keeps working as normal, and the tool tells you what went wrong:

- On the **settings page**, a red box at the top lists recent problems. Press **Copy report** and send the text to the developer.
- In the **companion's map window**, a small notice at the bottom right points you to the settings page. **Hide** closes it until the page reloads.
- If the detector fails three times in a row and can't restart, **Windows shows a message** that explains how to send a report.

## Run the doctor

For most problems, run this in the tool's folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
```

It checks the companion, its DevTools setting and port, the autorun task, the watcher, the detector and the settings page, and suggests a fix for each problem it finds. Apply the first fix, then run it again. Add `-Report` to save a report you can share in `data\`.

## "No DevTools feed on port 9231"

If the log says this, the companion was already running when you installed the tool. Close it (tray icon > Exit) and open it again.

## Log files

| Log | What's in it |
|---|---|
| `data\logs\errors.log` | Every warning and error from every part of the tool. Start here. |
| `data\logs\detector.log` | Everything the detector logs: the current file plus up to 5 older ones, 2 MB each. |
| `data\install.log` | The output of the last install. |
| `data\autorun.log` | Whether the watcher saw the companion and its DevTools port. |
| `data\update.log` | Automatic updates: checks, downloads, installs and rollbacks. |
| `data-run.log` | The detector's log. |
| `data-run.prev.log` | The log from the detector's previous run, kept after a restart. |
