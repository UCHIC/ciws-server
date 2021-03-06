import re
import sys
import csv
import json
import logging
import pandas as pd

from pathlib import Path, PosixPath
from typing import Dict, Any, TextIO, List, Tuple, Union
from influxdb import DataFrameClient
from influxdb.exceptions import InfluxDBClientError, InfluxDBServerError
from requests.exceptions import ConnectionError, ReadTimeout

from common import create_logger

measurement_name_map = {
    None: 'RawData',
    'QC': 'QCData'
}


def process_source_files():
    """
    Main function that reads all the csv files, creates a dataframe with the data, and uploads to influxdb.
    """
    logger.info("starting loading service.")

    # get the source and target directories from the settings file.
    source, target, quarantine = setup_directories()

    # get list of all csv files.
    csv_files: List[Path] = sorted(source.glob('*.csv'))
    if type(Path('.')) == PosixPath:
        csv_files += sorted(source.glob('*.CSV'))

    # stop process if there are no files.
    if len(csv_files) == 0:
        logger.info('no files found in the source directory.')
        return

    logger.info(f'found {len(csv_files)} files in {source}.')

    # create the influxdb connection.
    influx_client: DataFrameClient = create_influx_client()

    for csv_file_path in csv_files:
        # get all the metadata from the csv file.
        try:
            site_id, datalogger_id, is_qc = get_file_metadata(csv_file_path)
        except (AttributeError, StopIteration) as err:
            logger.exception(f'The csv file {csv_file_path.name} is not formatted correctly: {err}')
            logger.info(f'error while parsing file {csv_file_path.name}, moving to {quarantine}')
            move_file(csv_file_path, quarantine)
            continue

        measurement_name = measurement_name_map.get(is_qc)
        logger.info(f'working on file {csv_file_path.name} with site id: {site_id} and datalogger id: {datalogger_id}.')

        # create the pandas dataframe with the data in the csv file.
        try:
            csv_dataframe: pd.DataFrame = generate_dataframe(csv_file_path)
        except (IOError, ValueError) as err:
            logger.exception(f'error generating dataframe for file {csv_file_path.name}: {err}')
            logger.info(f'error while parsing file {csv_file_path.name}, moving to {quarantine}')
            move_file(csv_file_path, quarantine)
            continue

        # insert all the data into the influxdb instance.
        try:
            success: bool = insert_influx_dataframe(influx_client, csv_dataframe, measurement_name, site_id, datalogger_id)
        except (ConnectionError, ReadTimeout, InfluxDBClientError, InfluxDBServerError) as e:
            logger.exception(f'error connecting to the influxdb instance: {e}')
            continue
        if not success:
            logger.info('data could not be uploaded to influx.')
            continue
        logger.info('data uploaded to influx successfully.')

        # move the csv file from the source directory to the target directory
        target_file: Path = move_file(csv_file_path, target)
        if target_file.exists():
            logger.info(f'file {csv_file_path.name} has been moved to the target directory at {target}.')
        else:
            logger.info(f'file {csv_file_path.name} could not be moved to the target directory.')

    logger.info('process complete!!')


def setup_directories() -> Tuple[Path, Path, Path]:
    """
    Get the source and target directory from the settings.json file and create the directories if they don't exist.
    """

    source: Path = Path(config.get('source_directory'))
    target: Path = Path(config.get('target_directory'))
    quarantine: Path = Path(config.get('quarantine_directory'))

    source.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)
    logger.debug(f'source: {source}, target: {target}, and quarantine: {quarantine} directories loaded.')
    return source, target, quarantine


def create_influx_client() -> DataFrameClient:
    """
    Creates a Dataframe InfluxDB Client with the connection data from the settings.json file.
    """
    database: Dict[str, Any] = config.get('database')
    user = database.get('user')
    password = database.get('password')
    dbname = database.get('name')
    port = database.get('port')
    host = database.get('host')
    logger.debug(f'connection info: {database}')
    logger.info(f'creating influx connection as {user} to {dbname} at {host}.')

    return DataFrameClient(host, port, user, password, dbname)


def generate_dataframe(csv_file_path: Path) -> pd.DataFrame:
    """
    Reads a CSV file and creates a Pandas Dataframe.
    """
    logger.debug(f'creating dataframe for file {csv_file_path.name}.')
    data_frame: pd.DataFrame = pd.read_csv(
        csv_file_path,
        index_col=0,
        sep=',',
        date_parser=parse_date,
        header=3,
        usecols=["Time", "Pulses"]
    )
    data_frame.rename(columns={'Pulses': 'pulses'}, inplace=True)

    logger.info(f'{len(data_frame)} rows read from csv file {csv_file_path.name}')
    return data_frame


def parse_metadata(metadata) -> Tuple[str, str]:
    """
    Parse a metadata line in the csv files with format "FIELD: ID"
    """
    match = re.search(r'^([a-zA-Z:0# ]*)(?P<id>\d+)(?P<qc>QC)?', metadata)
    return match.group('id'), match.group('qc')


def get_file_metadata(csv_file_path: Path) -> Tuple[str, str, Union[str, None]]:
    """
    Get the first 3 lines of metadata (site id, datalogger id, meterin a CSV file.
    """
    csv_file: TextIO
    with csv_file_path.open(newline='') as csv_file:
        reader = csv.reader(csv_file)
        site_metadata = next(reader)[0]
        datalogger_metadata = next(reader)[0]
        meter_metadata = next(reader)[0]

    site_id, is_qc = parse_metadata(site_metadata)
    datalogger_id = parse_metadata(datalogger_metadata)[0]
    return site_id, datalogger_id, is_qc


def insert_influx_dataframe(
        client: DataFrameClient, dataframe: pd.DataFrame,
        measurement_name: str, site_id: str, datalogger_id: str) -> bool:
    """
    Insert all csv data as a dataframe into influxdb client.
    """
    logger.debug('inserting rows into influxdb.')
    success: bool = client.write_points(
        dataframe=dataframe,
        measurement=measurement_name,
        field_columns={
            'pulses': dataframe[['pulses']]
        },
        tags={
            'siteID': site_id,
            'dataloggerID': datalogger_id
        },
    )

    return success


def move_file(csv_file_path: Path, target_directory: Path) -> Path:
    """
    Move a CSV file a file to a new directory.
    """
    logger.debug(f'moving file {csv_file_path.name} to {target_directory}')
    target_file = Path(target_directory / csv_file_path.name)
    csv_file_path.rename(target_file)
    return target_file


def parse_date(date_string: str):
    """
    Parses datetime objects in dataframe.
    """
    return pd.to_datetime(date_string, yearfirst=True)

# def send_error(data):
#     message = {
#         "text": data
#     }
#     response = requests.post(
#         url=config['slack_webhook'],
#         data=json.dumps(message),
#         headers={'Content-Type': 'application/json'}
#     )
#     if response.status_code != 200:
#         raise ValueError(
#             'Request to slack returned an error %s, the response is:\n%s'
#             % (response.status_code, response.text)
#         )


if __name__ == "__main__":
    settings_path = Path(__file__).resolve().parent.parent / 'settings.json'
    config: Dict[str, Any] = {}
    data_file: TextIO

    try:
        with settings_path.open() as data_file:
            config = json.load(data_file)
    except OSError:
        sys.exit("Unable to read settings.json.")

    logger: logging.Logger = create_logger('data_loader', config.get('log_directory'))
    process_source_files()
