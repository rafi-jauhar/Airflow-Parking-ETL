#!/usr/bin/env python
"""Sample ETL Pipeline DAG script for Parking Company.

Simulates Extract, Transform, and Load processes of customer parking data.
Data is requested from API connection made in Airflow and stored in XCOM in JSON format.
JSON data is retrieved from XCOM and transformed accordingly using Pandas DataFrame.
This DataFrame is stored in JSON or CSV format and loaded into PostgreSQL database.
"""

import os
from airflow import DAG
from airflow.providers.http.sensors.http import HttpSensor
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.operators.bash import BashOperator
from airflow.providers.http.operators.http import SimpleHttpOperator
from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.operators.dummy import DummyOperator
from airflow.models import Variable
from airflow.providers.postgres.hooks.postgres import PostgresHook
import requests
import json
import pandas as pd
from datetime import datetime, timedelta

# Grab current date
current_date = datetime.today().strftime('%Y-%m-%d')

# Default settings for all the dags in the pipeline
default_args = {
    "owner": "Airflow", 
    "start_date": datetime(2022, 8, 1),
    "retries": 1,
    "retry_delay": timedelta(minutes=10)
}

def _transform_parking_data(ti):
    """
    Transforms the parking data into a DataFrame and prepares it for storage.
    """
    parking_data = ti.xcom_pull(task_ids=['extract_user'])  # Extract the json object from XCom

    if not parking_data:
        raise Exception('No processed parking data.')  # Check if parking_data is empty

    # Convert the parking data into a pandas dataframe
    data = json.dumps(parking_data[0])
    jsonDict = json.loads(data)
    df = pd.json_normalize(jsonDict)
    df = df.set_index('parkingTransactionKey')  # Index becomes transaction key

    # Drop irrelevant columns
    df = df.drop(['transactionSourceCode', 'meterId', 'zoneNbr', 'meterManufacturerName', 'blockNbr', 
                  'sourceStreetDisplayName', 'sideDirectionName', 'latitudeCrd', 'longitudeCrd', 
                  'statePlaneXCrd', 'statePlaneYCrd', 'handicapInd', 'timeRestrictionDsc', 
                  'zoneSpaceCnt'], axis=1)

    # Convert DataFrame to JSON string for XCom compatibility
    return df.to_json(orient='split')

def _store_data_csv(ti):
    """
    Stores the transformed data in CSV format.
    """
    df_json = ti.xcom_pull(task_ids='transform_data')
    df = pd.read_json(df_json, orient='split')
    csv_path = Variable.get('parking_data_csv_path')
    filename = os.path.join(csv_path, "processed_parking_data_00.csv")

    # Ensure the CSV directory exists
    os.makedirs(csv_path, exist_ok=True)
    
    # Store Parking Data in CSV format
    df.to_csv(filename, index=False, mode='a', header=not os.path.exists(filename))

def _store_data_json(ti):
    """
    Stores the transformed data in JSON format and ignores any errors.
    """
    try:
        # Retrieve DataFrame from the XCom
        df = ti.xcom_pull(task_ids='transform_data')
        
        # Define JSON storage path
        json_path = Variable.get('parking_data_json_path')
        filename = os.path.join(json_path, "processed_parking_data.json")
        
        # Ensure the JSON directory exists
        os.makedirs(json_path, exist_ok=True)
        
        # Store Data in JSON format
        df.to_json(filename, orient='records', lines=True)
        print("Data successfully stored in JSON format.")
        
    except Exception as e:
        # Log the exception and continue to the next task
        print(f"Error storing data in JSON format: {e}. Task will continue.")

def _load_parking_data():
    """
    This function uses the Postgres Hook to copy users from processed_data.csv
    and into the table.
    """
    # Connect to the Postgres connection
    hook = PostgresHook(postgres_conn_id='postgres')
    
    # Retrieve csv path from airflow variable
    csv_path = Variable.get('parking_data_csv_path')
    filename = os.path.join(csv_path, "processed_parking_data_00.csv")
    
    # Insert the data from the CSV file into the postgres database
    hook.copy_expert(
        sql="COPY parking_data FROM stdin WITH DELIMITER as ','",
        filename=filename
    )

with DAG('parking_pipeline', default_args=default_args, schedule_interval=timedelta(minutes=2), catchup=False) as dag:

    # Task #1 - Check if the API is available
    is_api_available = HttpSensor(
        task_id='is_api_available',
        method='GET',
        http_conn_id='is_api_available',
        endpoint=''
    )

    # Task #2 - Create a table
    create_table = PostgresOperator(
        task_id='create_table',
        postgres_conn_id='postgres',
        sql='''
            DROP TABLE IF EXISTS parking_data;
            CREATE TABLE parking_data (
                startDtm TEXT NOT NULL,
                endDtm TEXT NOT NULL,
                transactionAmt TEXT NOT NULL,
                paymentTypeName TEXT NOT NULL,
                transactionStatusCode TEXT NOT NULL,
                maxHoursCnt TEXT NOT NULL,
                meterTypeDsc TEXT NOT NULL,
                dollarPerHourRate TEXT NOT NULL,
                activeStatusInd TEXT NOT NULL,
                metroAreaName TEXT NOT NULL
            );
        '''
    )

    # Task #3 - Extract Data
    extract_data = SimpleHttpOperator(
        task_id='extract_user',
        http_conn_id='is_api_available',
        method='GET',
        endpoint='?$top=50',
        response_filter=lambda response: json.loads(response.text),
        log_response=True
    )

    # Task #4 - Transform Data
    transform_data = PythonOperator(
        task_id='transform_data',
        python_callable=_transform_parking_data
    )

    # Task #5 - Store Data in CSV Format
    store_data_csv = PythonOperator(
        task_id='store_data_csv',
        python_callable=_store_data_csv
    )

    # Task #6 - Store Data in JSON Format (continue even if it fails)
    store_data_json = PythonOperator(
        task_id='store_data_json',
        python_callable=_store_data_json
    )

    # Dummy task to bypass any result from store_data_json before load
    bypass_json = DummyOperator(
        task_id='bypass_json'
    )

    # Task #7 - Load Data
    load_data = PythonOperator(
        task_id='load_data',
        python_callable=_load_parking_data,
        trigger_rule="all_done"  # Run this task even if the upstream task fails
    )


    # Dependencies
    is_api_available >> create_table >> extract_data >> transform_data
    transform_data >> [store_data_csv, store_data_json]  # Parallel storage to both CSV and JSON
    store_data_json >> bypass_json >> load_data  # store_data_json output is ignored
    store_data_csv >> load_data  # Load data only from CSV