import os, datetime, ipaddress, uuid, time, random, string, logging, itertools, reprlib
from collections import namedtuple
from .exceptions import (Warning, Error, InterfaceError, DatabaseError, DataError, OperationalError, IntegrityError,
                         InternalError, ProgrammingError, NotSupportedError)
import boto3

apilevel = "2.0"

threadsafety = 0

paramstyle = "named"

Date = datetime.date
Time = datetime.time
Timestamp = datetime.datetime
DateFromTicks = datetime.date.fromtimestamp
# TimeFromTicks = datetime.time.fromtimestamp TODO
TimestampFromTicks = datetime.datetime.fromtimestamp
Binary = bytes
STRING = str
BINARY = bytes
NUMBER = float
DATETIME = datetime.datetime
ROWID = str

ColumnDescription = namedtuple("ColumnDescription", "name type_code display_size internal_size precision scale null_ok")
ColumnDescription.__new__.__defaults__ = (None,) * len(ColumnDescription._fields)

logger = logging.getLogger(__name__)


class AuroraDataAPIClient:
    def __init__(self, dbname=None, aurora_cluster_arn=None, secret_arn=None, rds_data_client=None):
        self._client = rds_data_client
        if rds_data_client is None:
            self._client = boto3.client("rds-data")
        self._dbname = dbname
        self._aurora_cluster_arn = aurora_cluster_arn or os.environ.get("AURORA_CLUSTER_ARN")
        self._secret_arn = secret_arn or os.environ.get("AURORA_SECRET_ARN")
        self._transaction_id = None

    def close(self):
        pass

    def commit(self):
        if self._transaction_id:
            res = self._client.commit_transaction(resourceArn=self._aurora_cluster_arn,
                                                  secretArn=self._secret_arn,
                                                  transactionId=self._transaction_id)
            self._transaction_id = None
            if res["transactionStatus"] != "Transaction Committed":
                raise DatabaseError("Error while committing transaction: {}".format(res))

    def rollback(self):
        if self._transaction_id:
            self._client.rollback_transaction(resourceArn=self._aurora_cluster_arn,
                                              secretArn=self._secret_arn,
                                              transactionId=self._transaction_id)
            self._transaction_id = None

    def cursor(self):
        if self._transaction_id is None:
            res = self._client.begin_transaction(database=self._dbname,
                                                 resourceArn=self._aurora_cluster_arn,
                                                 # schema="string", TODO
                                                 secretArn=self._secret_arn)
            self._transaction_id = res["transactionId"]
        return AuroraDataAPICursor(client=self._client,
                                   dbname=self._dbname,
                                   aurora_cluster_arn=self._aurora_cluster_arn,
                                   secret_arn=self._secret_arn,
                                   transaction_id=self._transaction_id)

    def __enter__(self):
        return self

    def __exit__(self, err_type, value, traceback):
        if err_type is not None:
            self.rollback()
        else:
            self.commit()


class AuroraDataAPICursor:
    _pg_type_map = {
        "int": int,
        "int2": int,
        "int4": int,
        "int8": int,
        "float4": float,
        "float8": float,
        "serial2": int,
        "serial4": int,
        "serial8": int,
        "bool": bool,
        "varbit": bytes,
        "bytea": bytearray,
        "char": str,
        "varchar": str,
        "cidr": ipaddress.ip_network,
        "date": datetime.date,
        "inet": ipaddress.ip_address,
        "json": dict,  # TODO
        "jsonb": dict,  # TODO
        "money": str,  # TODO
        "text": str,
        "time": datetime.time,
        "timestamp": datetime.datetime,
        "uuid": uuid.uuid4
    }
    _data_api_type_map = {
        bytes: "blobValue",
        bool: "booleanValue",
        float: "doubleValue",
        # isNull=None, TODO
        int: "longValue",
        str: "stringValue"
    }

    def __init__(self, client=None, dbname=None, aurora_cluster_arn=None, secret_arn=None, transaction_id=None):
        self.arraysize = 1000
        self.description = None
        self._client = client
        self._dbname = dbname
        self._aurora_cluster_arn = aurora_cluster_arn
        self._secret_arn = secret_arn
        self._transaction_id = transaction_id
        self._iterator = None
        self._current_response = None
        self._paging_state = None

    def prepare_param_value(self, param_value):
        param_data_api_type = self._data_api_type_map.get(type(param_value), "stringValue")
        return {param_data_api_type: param_value}

    def _set_description(self, column_metadata):
        # see https://www.postgresql.org/docs/9.5/datatype.html
        self.description = []
        for column in column_metadata:
            col_desc = ColumnDescription(name=column["name"],
                                         type_code=self._pg_type_map.get(column["typeName"], str))
            self.description.append(col_desc)

    def _start_paginated_query(self, execute_statement_args, records_per_page=None):
        pg_cursor_name = "{}_{}_{}".format(__name__,
                                           int(time.time()),
                                           ''.join(random.choices(string.ascii_letters + string.digits, k=8)))
        cursor_stmt = "DECLARE " + pg_cursor_name + " SCROLL CURSOR FOR "
        execute_statement_args["sql"] = cursor_stmt + execute_statement_args["sql"]
        self._client.execute_statement(**execute_statement_args)
        self._paging_state = {
            "execute_statement_args": dict(execute_statement_args),
            "records_per_page": records_per_page or self.arraysize,
            "pg_cursor_name": pg_cursor_name
        }

    def _prepare_execute_args(self, operation):
        execute_args = dict(database=self._dbname,
                            resourceArn=self._aurora_cluster_arn,
                            secretArn=self._secret_arn,
                            sql=operation)
        if self._transaction_id:
            execute_args["transactionId"] = self._transaction_id
        return execute_args

    def _format_parameter_set(self, parameters):
        return [
            {"name": k, "value": self.prepare_param_value(v)}
            for k, v in parameters.items()
        ]

    def execute(self, operation, parameters=None):
        self._iterator, self._paging_state = None, None
        execute_statement_args = dict(self._prepare_execute_args(operation),
                                      includeResultMetadata=True)
        if parameters:
            execute_statement_args["parameters"] = self._format_parameter_set(parameters)
        logger.debug("execute %s", reprlib.repr(operation.strip()))
        try:
            res = self._client.execute_statement(**execute_statement_args)
            if "columnMetadata" in res:
                self._set_description(res["columnMetadata"])
            self._current_response = self._render_response(res)
        except self._client.exceptions.BadRequestException as e:
            if "Please paginate your query" in str(e):
                self._start_paginated_query(execute_statement_args)
            elif "Database response exceeded size limit" in str(e):
                self._start_paginated_query(execute_statement_args, records_per_page=max(1, self.arraysize // 2))
            else:
                raise
        self._iterator = iter(self)

    @property
    def rowcount(self):
        if self._current_response and "records" in self._current_response:
            return len(self._current_response["records"])  # TODO: also get rowcount on atomic INSERT/UPDATE
        return -1

    def _page_input(self, iterable, page_size=1000):
        iterable = iter(iterable)
        return iter(lambda: list(itertools.islice(iterable, page_size)), [])

    def executemany(self, operation, seq_of_parameters):
        logger.debug("executemany %s", reprlib.repr(operation))
        for batch in self._page_input(seq_of_parameters):
            batch_execute_statement_args = dict(self._prepare_execute_args(operation),
                                                parameterSets=[self._format_parameter_set(p) for p in batch])
            self._client.batch_execute_statement(**batch_execute_statement_args)

    def _render_response(self, response):
        if "records" in response:
            for i, record in enumerate(response["records"]):
                response["records"][i] = tuple(self._render_value(v) for v in response["records"][i])
        return response

    def _render_value(self, value):
        if value.get("isNull"):
            return None
        elif "arrayValue" in value:
            if "arrayValues" in value["arrayValue"]:
                return [self._render_value(nested) for nested in value["arrayValue"]["arrayValues"]]
            else:
                return list(value["arrayValue"].values())[0]
        else:
            return list(value.values())[0]

    def scroll(self, value, mode="relative"):
        if not self._paging_state:
            raise InterfaceError("Cursor scroll attempted but pagination is not active")
        scroll_stmt = "MOVE {mode} {value} FROM {pg_cursor_name}".format(
            mode=mode.upper(),
            value=value,
            **self._paging_state
        )
        scroll_args = dict(self._paging_state["execute_statement_args"], sql=scroll_stmt)
        logger.debug("Scrolling cursor %s %d", mode, value)
        self._client.execute_statement(**scroll_args)

    def __iter__(self):
        if self._paging_state:
            next_page_args = self._paging_state["execute_statement_args"]
            while True:
                logger.debug("Fetching page of %d records for auto-paginated query",
                             self._paging_state["records_per_page"])
                next_page_args["sql"] = "FETCH {records_per_page} FROM {pg_cursor_name};".format(**self._paging_state)
                try:
                    page = self._client.execute_statement(**next_page_args)
                except self._client.exceptions.BadRequestException as e:
                    if "Database response exceeded size limit" in str(e) and self._paging_state["records_per_page"] > 1:
                        self.scroll(-self._paging_state["records_per_page"])  # Rewind the cursor to read the page again
                        logger.debug("Halving records per page")
                        self._paging_state["records_per_page"] //= 2
                        continue
                    else:
                        raise

                if "columnMetadata" in page and not self.description:
                    self._set_description(page["columnMetadata"])
                if len(page["records"]) == 0:
                    break
                page = self._render_response(page)
                for record in page["records"]:
                    yield record
        else:
            for record in self._current_response.get("records", []):
                yield record

    def fetchone(self):
        try:
            return next(self._iterator)
        except StopIteration:
            pass

    def fetchmany(self, size=None):
        if size is None:
            size = self.arraysize
        results = []
        while size > 0:
            result = self.fetchone()
            if result is None:
                break
            results.append(result)
            size -= 1
        return results

    def fetchall(self):
        return list(self._iterator)

    def setinputsizes(self, sizes):
        pass

    def setoutputsize(self, size, column=None):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, err_type, value, traceback):
        self._iterator = None
        self._current_response = None


def connect(aurora_cluster_arn=None, secret_arn=None, database=None, host=None, username=None, password=None):
    return AuroraDataAPIClient(dbname=database, aurora_cluster_arn=aurora_cluster_arn, secret_arn=secret_arn)
