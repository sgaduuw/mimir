"""The one Flask blueprint every route, filter, and hook in the
package attaches to. Lives in its own module so that any submodule
can import it without circular dependencies on the package
`__init__.py` (which itself imports submodules to trigger
registration).
"""
from flask import Blueprint

bp_web = Blueprint("web", __name__)
