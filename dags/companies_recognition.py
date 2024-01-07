from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.postgres_operator import PostgresOperator
from airflow.hooks.postgres_hook import PostgresHook
from airflow.models import Variable
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import logging
import easyocr
from peopledatalabs import PDLPY

# Define a function to extract image URLs from a website
def extract_image_urls():
    target_url = Variable.get("TARGET_WEBSITE")
    response = requests.get(target_url)
    soup = BeautifulSoup(response.content, 'html.parser')
    image_urls = {img['src'] for img in soup.find_all('img') if 'src' in img.attrs}

    pg_hook = PostgresHook(postgres_conn_id='postgres_conn_id')
    existing_urls = {row[0] for row in pg_hook.get_records("SELECT url FROM images")}
    new_urls = image_urls - existing_urls

    for url in new_urls:
        pg_hook.run("INSERT INTO images (url, processed) VALUES (%s, false)", parameters=(url,))

# Function to process the images using OCR
def ocr_process_images():
    pg_hook = PostgresHook(postgres_conn_id='postgres_conn_id')
    unprocessed_images = pg_hook.get_records("SELECT url FROM images WHERE processed = false")
    reader = easyocr.Reader(['en'])
    urls_to_process = []

    for row in unprocessed_images:
        try:
            image_url = row[0]
            ocr_results = reader.readtext(image_url)
            extracted_text = ' '.join([result[1] for result in ocr_results])
            urls_to_process.append(extracted_text)

            pg_hook.run("UPDATE images SET processed = true WHERE url = %s", parameters=(image_url,))
        except Exception as e:
            logging.error(f"Error with image {image_url}: {e}")

    return urls_to_process


# Function to enrich company information
def enrich_company_data():
    urls = ti.xcom_pull(task_ids='ocr_process_images')
    pdl_client = PDLPY(api_key=Variable.get("PDL_API_KEY"))
    pg_hook = PostgresHook(postgres_conn_id='postgres_conn_id')

    for url in urls:
        try:
            response = pdl_client.company.enrichment(website=url)
            company_data = response.json()
            insert_query = """
                INSERT INTO company_data (url, name, display_name, size, founded_year)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (url) DO NOTHING
            """
            pg_hook.run(insert_query, parameters=(url, company_data['name'], company_data['display_name'], company_data['size'], company_data['founded']))
        except Exception as e:
            logging.error(f"Error enriching data for {url}: {e}")

# DAG definition
with DAG('marketing_material_ingestion', start_date=datetime(2023, 12, 9), schedule_interval="@daily", catchup=False) as dag:
    setup_image_table = PostgresOperator(
        task_id='setup_image_table',
        postgres_conn_id='postgres_conn_id',
        sql="""
            CREATE TABLE IF NOT EXISTS images (
                id SERIAL PRIMARY KEY,
                url TEXT NOT NULL,
                processed BOOLEAN NOT NULL DEFAULT false
            );
        """
    )

    setup_company_table = PostgresOperator(
        task_id='setup_company_table',
        postgres_conn_id='postgres_conn_id',
        sql="""
            CREATE TABLE IF NOT EXISTS company_data (
                url VARCHAR(255) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                display_name VARCHAR(255),
                size VARCHAR(255),
                founded_year INT
            );
        """
    )

    image_extraction = PythonOperator(
        task_id='extract_image_urls',
        python_callable=extract_image_urls
    )

    ocr_processing = PythonOperator(
        task_id='ocr_process_images',
        python_callable=ocr_process_images
    )

    company_enrichment = PythonOperator(
        task_id='enrich_company_data',
        python_callable=enrich_company_data
    )

    setup_image_table >> setup_company_table >> image_extraction >> ocr_processing >> company_enrichment