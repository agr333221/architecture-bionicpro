FROM apache/airflow:2.10.5-python3.11
RUN pip install --no-cache-dir clickhouse-connect==0.8.11
COPY dags /opt/airflow/dags
