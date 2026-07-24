"""`python -m vault` → host-side CLI (serve/policy). См. vault/cli.py."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
