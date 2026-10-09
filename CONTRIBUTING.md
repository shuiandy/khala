# Contributing

Thanks for helping. Bug reports, fixes and new client integrations are all welcome.

## Setting up

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests
```

The tests need Git on the `PATH` and take about a minute. They run a real server in process, with a temporary
repository, a fake mailer and a software authenticator for passkeys.

## Making a change

- Open an issue first for anything larger than a fix, so we can agree on the approach.
- Keep pull requests focused on one change, and add a test that fails without it. Access control changes need
  tests for what must stay hidden, not only for what becomes visible.
- Match the surrounding code: standard library first, few dependencies, imports at the top of the module, short
  comments that explain why.
- Comments, docstrings and commit messages are in English. User-facing text is in English.
- Commit messages: an imperative summary line under 72 characters, then a body that says why.

## Reporting bugs

Include the version or commit, what you did, what you expected and what happened. Leave out record contents,
tokens and sign-in codes. Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## License

By contributing you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).
