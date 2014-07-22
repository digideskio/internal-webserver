"""Miscellaneous utilities for interacting with GAE."""

import os
import sys


def _discover_sdk_path():
    """Return directory from $PATH where the Google Appengine DSK lives."""
    # adapted from {http://code.google.com/p/bcannon/source/browse/
    # sites/py3ksupport-hrd/run_tests.py}

    # Poor-man's `which` command.
    for path in os.environ['PATH'].split(':'):
        if os.path.isdir(path) and 'dev_appserver.py' in os.listdir(path):
            # Follow symlinks to the real appengine install directory.
            realpath = os.path.realpath(os.path.join(path, 'dev_appserver.py'))
            path = os.path.dirname(realpath)
            break
    else:
        raise RuntimeError("couldn't find dev_appserver.py on $PATH")

    # Verify the App Engine installation directory looks right.
    assert os.path.isdir(os.path.join(path, 'google', 'appengine')), path
    return path


def fix_sys_path(appengine_sdk_dir=None):
    """Update sys.path for appengine khan academy imports, also envvars."""
    # This was originally copied  webapp/tools/appengine_tool_setup.py

    if 'SERVER_SOFTWARE' not in os.environ:
        os.environ['SERVER_SOFTWARE'] = 'Development'
    if 'CURRENT_VERSION' not in os.environ:
        os.environ['CURRENT_VERSION'] = '764.1'

    if not appengine_sdk_dir:
        appengine_sdk_dir = _discover_sdk_path()
    sys.path.append(appengine_sdk_dir)

    # We put this at the front of the path so 'import google' gets the
    # appengine 'google' module, not another, un-useful google module
    # that is installed as part of, say, the bigquery package.  In
    # fact, bigquery is so annoying it registers python to import the
    # (bad) google module at python-start time.  It uses pkgutil to do
    # that, so we can update __path__ to include our path if needed.
    for (module_name, module) in sys.modules.items():
        if module_name == 'google' and hasattr(module, '__path__'):
            # Fake appengine apps that expect 'google' to just be the
            # appengine google.
            module.__file__ = os.path.join(appengine_sdk_dir, 'google')
            # Update the pkgutil __path__ var to say that google.foo
            # imports should look in the appengine-sdk first.  cf.
            # http://stackoverflow.com/questions/2699287/what-is-path-useful-for
            module.__path__.insert(0, module.__file__)
    sys.path.insert(0, appengine_sdk_dir)

    # Delegate the real work to the dev_appserver logic in the actual SDK.
    import dev_appserver
    dev_appserver.fix_sys_path()

