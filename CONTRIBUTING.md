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

Do not attach credentials, private network addresses, or full production databases.
For reference manuals and product photographs, record the public source, retrieval
date, and scope in [third-party references](docs/third-party.md). Keep manuals in
`docs/manuals/` and identify third-party assets separately from the MIT-licensed
project material. Hardware reports should distinguish published specifications,
owner confirmation, and measurements.
