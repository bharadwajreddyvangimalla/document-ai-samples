from typing import Dict, List
import json

from google.api_core.client_options import ClientOptions
from google.protobuf.json_format import ParseError

from google.cloud import documentai_v1 as documentai
from google.cloud import storage

from consts import (
    PROJECT_ID,
    LOCATION,
    PROCESSOR_ID,
    BATCH_MAX_FILES,
    TIMEOUT,
    ACCEPTED_MIME_TYPES,
)

storage_client = storage.Client()


def extract_document_entities(document: documentai.Document) -> dict:
    """
    Get all entities from a document and output as a dictionary
    Flattens nested entities/properties
    Format: entity.type_: entity.mention_text OR entity.normalized_value.text
    """
    document_entities: Dict[str, str] = {}

    for entity in document.entities:

        entity_key = entity.type_.replace("/", "_")
        normalized_value = getattr(entity, "normalized_value", None)

        entity_value = (
            normalized_value.text if normalized_value else entity.mention_text
        )

        document_entities.update({entity_key: entity_value})

    return document_entities


def create_batches(
    input_bucket: str, input_prefix: str, batch_size: int = BATCH_MAX_FILES
) -> List[List[documentai.GcsDocument]]:
    """
    Create batches of documents to process
    """
    if batch_size > BATCH_MAX_FILES:
        raise ValueError(
            f"Batch size must be less than {BATCH_MAX_FILES}. "
            f"You provided {batch_size}"
        )

    blob_list = storage_client.list_blobs(input_bucket, prefix=input_prefix)

    batches: List[List[documentai.GcsDocument]] = []
    batch: List[documentai.GcsDocument] = []

    for blob in blob_list:

        if blob.content_type not in ACCEPTED_MIME_TYPES:
            print(f"Invalid Mime Type {blob.content_type} - Skipping file {blob.name}")
            continue

        if len(batch) == batch_size:
            batches.append(batch)
            batch = []

        batch.append(
            documentai.GcsDocument(
                gcs_uri=f"gs://{input_bucket}/{blob.name}",
                mime_type=blob.content_type,
            )
        )

    batches.append(batch)

    return batches


def _batch_process_documents(
    gcs_output_uri: str,
    input_bucket: str,
    input_prefix: str,
    batch_size: int = BATCH_MAX_FILES,
) -> List[str]:
    """
    Constructs a request to process a document using the Document AI
    Batch Method.
    """
    docai_client = documentai.DocumentProcessorServiceClient(
        client_options=ClientOptions(
            api_endpoint=f"{LOCATION}-documentai.googleapis.com"
        )
    )
    resource_name = docai_client.processor_path(PROJECT_ID, LOCATION, PROCESSOR_ID)

    output_config = documentai.DocumentOutputConfig(
        gcs_output_config=documentai.DocumentOutputConfig.GcsOutputConfig(
            gcs_uri=gcs_output_uri
        )
    )

    batches = create_batches(input_bucket, input_prefix, batch_size)
    operation_ids: List[str] = []

    for i, batch in enumerate(batches):

        print(f"Processing Document Batch {i}: {len(batch)} documents")

        # Load GCS Input URI into a List of document files
        input_config = documentai.BatchDocumentsInputConfig(
            gcs_documents=documentai.GcsDocuments(documents=batch)
        )
        request = documentai.BatchProcessRequest(
            name=resource_name,
            input_documents=input_config,
            document_output_config=output_config,
        )

        operation = docai_client.batch_process_documents(request)
        operation_ids.append(operation.operation.name)

        print(f"Waiting for operation {operation.operation.name}")
        operation.result(timeout=TIMEOUT)

    return operation_ids


def get_document_protos_from_gcs(
    output_bucket: str, output_directory: str
) -> List[documentai.Document]:
    """
    Download document proto output from GCS. (Directory)
    """

    # List of all of the files in the directory
    # `gs://gcs_output_uri/operation_id`
    blob_list = storage_client.list_blobs(output_bucket, prefix=output_directory)

    document_protos = []

    for blob in blob_list:
        print("Fetching from " + blob.name)

        # Document AI should only output JSON files to GCS
        if ".json" in blob.name:
            # TODO: remove this when Document.from_json is fixed
            blob_data = blob.download_as_text()
            document_dict = json.loads(blob_data)

            # Remove large fields
            try:
                del document_dict["pages"]
                del document_dict["text"]
            except KeyError:
                pass
            try:
                document_proto = documentai.types.Document.from_json(
                    json.dumps(document_dict).encode("utf-8")
                )
            except ParseError:
                print(f"Failed to parse {blob.name}")
                continue

            document_protos.append(document_proto)
        else:
            print(f"Skipping non-supported file type {blob.name}")

    return document_protos


def cleanup_gcs(
    input_bucket: str,
    input_prefix: str,
    output_bucket: str,
    output_directory: str,
    archive_bucket: str,
):
    """
    Deleting the intermediate files created by the Doc AI Parser
    Moving Input Files to Archive
    """

    delete_queue = []

    # Intermediate document.json files
    intermediate_files = storage_client.list_blobs(
        output_bucket, prefix=output_directory
    )

    for blob in intermediate_files:
        delete_queue.append(blob)

    # Copy input files to archive bucket
    source_bucket = storage_client.bucket(input_bucket)
    destination_bucket = storage_client.bucket(archive_bucket)

    source_blobs = storage_client.list_blobs(input_bucket, prefix=input_prefix)

    for source_blob in source_blobs:
        print(
            f"Moving {source_bucket}/{source_blob.name} to {archive_bucket}/{source_blob.name}"
        )
        source_bucket.copy_blob(source_blob, destination_bucket, source_blob.name)
        delete_queue.append(source_blob)

    for blob in delete_queue:
        print(f"Deleting {blob.name}")
        blob.delete()