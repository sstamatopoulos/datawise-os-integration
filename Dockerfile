# The Airflow version and the Python version are written here and nowhere
# else. The constraints file is derived from the image itself, so bumping this
# one line (which is what Dependabot proposes) cannot leave the image enforcing
# another release's pins. Not hypothetical: the first Dependabot bump, to
# 3.3.2, changed this line and left a hard-coded 3.0.6 constraints URL behind;
# and the 3.3 images default to Python 3.13 while that URL said 3.12. The
# python suffix is explicit for the same reason.
FROM apache/airflow:3.3.2-python3.12

# Override only to test against an unreleased or patched constraint set.
ARG CONSTRAINTS_URL=""
RUN url="${CONSTRAINTS_URL:-https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])').txt}" \
 && echo "constraints: ${url}" \
 && curl -fsSL -o /tmp/constraints.txt "${url}"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --constraint /tmp/constraints.txt -r /tmp/requirements.txt

# The optional drivers of the built-in gates (Modbus, BACnet, OPC UA, SNMP,
# Excel, SFTP, MQTT, SQLAlchemy). Installed by default so that enabling a gate
# in config/gates.yaml is the only step needed; build with
# --build-arg INSTALL_GATE_EXTRAS=0 for a smaller image that runs only the
# HTTP, file and store gates.
#
# Installed under the Airflow constraints file, and that is not optional.
# Airflow 3.0.6 pinned SQLAlchemy==1.4.54; without the constraints, pip happily
# satisfied `SQLAlchemy>=2.0` from requirements-gates.txt and Airflow's own ORM
# then failed to import at all ("Type annotation for TaskInstance.dag_model
# can't be correctly interpreted"). The image built perfectly and every DAG
# inside it was dead. Constraints only restrict the packages they name, so
# pymodbus, BAC0, asyncua and pysnmp still resolve freely.
ARG INSTALL_GATE_EXTRAS=1
COPY requirements-gates.txt /tmp/requirements-gates.txt
RUN if [ "${INSTALL_GATE_EXTRAS}" = "1" ]; then \
      pip install --no-cache-dir --constraint /tmp/constraints.txt -r /tmp/requirements-gates.txt; \
    fi

# Fail the build rather than ship an image whose Airflow cannot import. This
# is the check that would have caught the above at build time instead of at
# the first DAG parse.
RUN python -c "import airflow, sqlalchemy; from airflow.models import TaskInstance; \
print('airflow', airflow.__version__, '| sqlalchemy', sqlalchemy.__version__)"
