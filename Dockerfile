FROM apache/airflow:3.3.2

ARG CONSTRAINTS_URL="https://raw.githubusercontent.com/apache/airflow/constraints-3.0.6/constraints-3.12.txt"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --constraint "${CONSTRAINTS_URL}" -r /tmp/requirements.txt

# The optional drivers of the built-in gates (Modbus, BACnet, OPC UA, SNMP,
# Excel, SFTP, MQTT, SQLAlchemy). They are installed by default so that
# enabling a gate in config/gates.yaml is the only step needed; build with
# --build-arg INSTALL_GATE_EXTRAS=0 for a smaller image that runs only the
# HTTP, file and store gates. They are installed without the Airflow
# constraints file, which does not know these packages; pip still refuses a
# version that conflicts with what Airflow itself needs.
ARG INSTALL_GATE_EXTRAS=1
COPY requirements-gates.txt /tmp/requirements-gates.txt
RUN if [ "${INSTALL_GATE_EXTRAS}" = "1" ]; then pip install --no-cache-dir -r /tmp/requirements-gates.txt; fi
