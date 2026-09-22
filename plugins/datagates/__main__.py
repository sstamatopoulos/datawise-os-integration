"""`python -m datagates ...` — see datagates.cli.

The guard checks the package, not only the name, and that is not pedantry.
This file sits in Airflow's plugins folder, and Airflow's plugin manager
imports every .py file it finds there under a module name taken from the
file's *basename* — which for this file is literally `__main__`. So the
usual `if __name__ == "__main__"` is true while Airflow is merely scanning,
and the CLI then parses Airflow's own argv:

    $ airflow users list
    datagates: error: argument command: invalid choice: 'users'

`python -m datagates` sets __package__ to "datagates"; Airflow's loader,
importing a file directly, leaves it empty. plugins/.airflowignore keeps
Airflow away from this file as well, which is the mechanism meant for it;
this guard is the belt to that braces, because an .airflowignore is easy to
lose in a refactor and this failure is very hard to read.
"""
import sys

from datagates.cli import main

if __name__ == "__main__" and __package__ == "datagates":
    sys.exit(main())
