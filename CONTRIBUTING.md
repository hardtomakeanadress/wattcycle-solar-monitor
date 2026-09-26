# Contributing

Bug reports, documentation corrections, and carefully verified hardware reports
are welcome. Include the controller's exact model response, adapter type,
Linux/Python versions, and a small redacted error or packet example.

For another controller model, provide evidence of its register layout before
relaxing the reader's model check. RS485 and Modbus support alone do not imply
that two controllers share addresses or scaling.

Keep the runtime dependent only on Python's standard library. Preserve
read-only controller access, single-reader serial ownership, visible failure
states, and indefinite retention of existing samples.

Before submitting a pull request:

```sh
python3 -m unittest discover -s dashboard -p 'test_*.py' -v
node scripts/test_frontend.cjs
```

Node.js is needed only for the optional frontend development tests. Mocked
tests do not prove compatibility with a new device; clearly distinguish a
hardware test from simulated responses.

For optional real-browser checks, install Playwright in a separate development
environment and run `scripts/test_browser.cjs` with `DASHBOARD_TEST_URL` pointing
to a running dashboard. Set `CHROME_PATH` to an existing Chromium/Chrome binary
if needed and `BROWSER_TEST_OUTPUT` to a screenshot directory. The script uses
only GET requests and intercepts the candidate HTML inside its isolated browser;
it does not install files on the server or start a serial reader. It expects
recent saved data and checks desktop/mobile layout, chart navigation, date
editing, point inspection, and recovery from a simulated HTTP failure.
Set `TEST_DEPLOYED=1` to test the server's actual HTML instead of the local candidate.

Do not attach credentials, private network addresses, or full production databases.
For reference manuals and product photographs, record the public source, retrieval
date, and scope in [third-party references](docs/third-party.md). Keep manuals in
`docs/manuals/` and identify third-party assets separately from the MIT-licensed
project material. Hardware reports should distinguish published specifications,
owner confirmation, and measurements.
