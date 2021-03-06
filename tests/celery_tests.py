"""Unit tests for Caravel Celery worker"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import imp
import json
import os
import subprocess
import time
import unittest

import pandas as pd

import caravel
from caravel import app, appbuilder, db, models, sql_lab, utils, dataframe

from .base_tests import CaravelTestCase

QueryStatus = models.QueryStatus

BASE_DIR = app.config.get('BASE_DIR')
cli = imp.load_source('cli', BASE_DIR + '/bin/caravel')


class CeleryConfig(object):
    BROKER_URL = 'sqla+sqlite:///' + app.config.get('SQL_CELERY_DB_FILE_PATH')
    CELERY_IMPORTS = ('caravel.sql_lab', )
    CELERY_RESULT_BACKEND = 'db+sqlite:///' + app.config.get('SQL_CELERY_RESULTS_DB_FILE_PATH')
    CELERY_ANNOTATIONS = {'sql_lab.add': {'rate_limit': '10/s'}}
    CONCURRENCY = 1
app.config['CELERY_CONFIG'] = CeleryConfig


class UtilityFunctionTests(CaravelTestCase):

    # TODO(bkyryliuk): support more cases in CTA function.
    def test_create_table_as(self):
        select_query = "SELECT * FROM outer_space;"
        updated_select_query = sql_lab.create_table_as(
            select_query, "tmp")
        self.assertEqual(
            "CREATE TABLE tmp AS \nSELECT * FROM outer_space;",
            updated_select_query)

        updated_select_query_with_drop = sql_lab.create_table_as(
            select_query, "tmp", override=True)
        self.assertEqual(
            "DROP TABLE IF EXISTS tmp;\n"
            "CREATE TABLE tmp AS \nSELECT * FROM outer_space;",
            updated_select_query_with_drop)

        select_query_no_semicolon = "SELECT * FROM outer_space"
        updated_select_query_no_semicolon = sql_lab.create_table_as(
            select_query_no_semicolon, "tmp")
        self.assertEqual(
            "CREATE TABLE tmp AS \nSELECT * FROM outer_space",
            updated_select_query_no_semicolon)

        multi_line_query = (
            "SELECT * FROM planets WHERE\n"
            "Luke_Father = 'Darth Vader';")
        updated_multi_line_query = sql_lab.create_table_as(
            multi_line_query, "tmp")
        expected_updated_multi_line_query = (
            "CREATE TABLE tmp AS \nSELECT * FROM planets WHERE\n"
            "Luke_Father = 'Darth Vader';")
        self.assertEqual(
            expected_updated_multi_line_query,
            updated_multi_line_query)


class CeleryTestCase(CaravelTestCase):
    def __init__(self, *args, **kwargs):
        super(CeleryTestCase, self).__init__(*args, **kwargs)
        self.client = app.test_client()

    def get_query_by_name(self, sql):
        session = db.session
        query = session.query(models.Query).filter_by(sql=sql).first()
        session.close()
        return query

    def get_query_by_id(self, id):
        session = db.session
        query = session.query(models.Query).filter_by(id=id).first()
        session.close()
        return query

    @classmethod
    def setUpClass(cls):
        try:
            os.remove(app.config.get('SQL_CELERY_DB_FILE_PATH'))
        except OSError as e:
            app.logger.warn(str(e))
        try:
            os.remove(app.config.get('SQL_CELERY_RESULTS_DB_FILE_PATH'))
        except OSError as e:
            app.logger.warn(str(e))

        utils.init(caravel)

        worker_command = BASE_DIR + '/bin/caravel worker'
        subprocess.Popen(
            worker_command, shell=True, stdout=subprocess.PIPE)

        admin = appbuilder.sm.find_user('admin')
        if not admin:
            appbuilder.sm.add_user(
                'admin', 'admin', ' user', 'admin@fab.org',
                appbuilder.sm.find_role('Admin'),
                password='general')
        cli.load_examples(load_test_data=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.call(
            "ps auxww | grep 'celeryd' | awk '{print $2}' | xargs kill -9",
            shell=True
        )
        subprocess.call(
            "ps auxww | grep 'caravel worker' | awk '{print $2}' | "
            "xargs kill -9",
            shell=True
        )

    def run_sql(self, dbid, sql, client_id, cta='false', tmp_table='tmp',
                async='false'):
        self.login()
        resp = self.client.post(
            '/caravel/sql_json/',
            data=dict(
                database_id=dbid,
                sql=sql,
                async=async,
                select_as_cta=cta,
                tmp_table_name=tmp_table,
                client_id=client_id,
            ),
        )
        self.logout()
        return json.loads(resp.data.decode('utf-8'))

    def test_add_limit_to_the_query(self):
        session = db.session
        db_to_query = session.query(models.Database).filter_by(
            id=1).first()
        eng = db_to_query.get_sqla_engine()

        select_query = "SELECT * FROM outer_space;"
        updated_select_query = db_to_query.wrap_sql_limit(select_query, 100)
        # Different DB engines have their own spacing while compiling
        # the queries, that's why ' '.join(query.split()) is used.
        # In addition some of the engines do not include OFFSET 0.
        self.assertTrue(
            "SELECT * FROM (SELECT * FROM outer_space;) AS inner_qry "
            "LIMIT 100" in ' '.join(updated_select_query.split())
        )

        select_query_no_semicolon = "SELECT * FROM outer_space"
        updated_select_query_no_semicolon = db_to_query.wrap_sql_limit(
            select_query_no_semicolon, 100)
        self.assertTrue(
            "SELECT * FROM (SELECT * FROM outer_space) AS inner_qry "
            "LIMIT 100" in
            ' '.join(updated_select_query_no_semicolon.split())
        )

        multi_line_query = (
            "SELECT * FROM planets WHERE\n Luke_Father = 'Darth Vader';"
        )
        updated_multi_line_query = db_to_query.wrap_sql_limit(multi_line_query, 100)
        self.assertTrue(
            "SELECT * FROM (SELECT * FROM planets WHERE "
            "Luke_Father = 'Darth Vader';) AS inner_qry LIMIT 100" in
            ' '.join(updated_multi_line_query.split())
        )

    def test_run_sync_query(self):
        main_db = db.session.query(models.Database).filter_by(
            database_name="main").first()
        eng = main_db.get_sqla_engine()

        # Case 1.
        # Table doesn't exist.
        sql_dont_exist = 'SELECT name FROM table_dont_exist'
        result1 = self.run_sql(1, sql_dont_exist, "1", cta='true')
        self.assertTrue('error' in result1)

        # Case 2.
        # Table and DB exists, CTA call to the backend.
        sql_where = "SELECT name FROM ab_permission WHERE name='can_sql'"
        result2 = self.run_sql(
            1, sql_where, "2", tmp_table='tmp_table_2', cta='true')
        self.assertEqual(QueryStatus.SUCCESS, result2['query']['state'])
        self.assertEqual([], result2['data'])
        self.assertEqual([], result2['columns'])
        query2 = self.get_query_by_id(result2['query']['serverId'])

        # Check the data in the tmp table.
        df2 = pd.read_sql_query(sql=query2.select_sql, con=eng)
        data2 = df2.to_dict(orient='records')
        self.assertEqual([{'name': 'can_sql'}], data2)

        # Case 3.
        # Table and DB exists, CTA call to the backend, no data.
        sql_empty_result = 'SELECT * FROM ab_user WHERE id=666'
        result3 = self.run_sql(
            1, sql_empty_result, "3", tmp_table='tmp_table_3', cta='true',)
        self.assertEqual(QueryStatus.SUCCESS, result3['query']['state'])
        self.assertEqual([], result3['data'])
        self.assertEqual([], result3['columns'])

        query3 = self.get_query_by_id(result3['query']['serverId'])
        self.assertEqual(QueryStatus.SUCCESS, query3.status)

    def test_run_async_query(self):
        main_db = db.session.query(models.Database).filter_by(
            database_name="main").first()
        eng = main_db.get_sqla_engine()

        # Schedule queries

        # Case 1.
        # Table and DB exists, async CTA call to the backend.
        sql_where = "SELECT name FROM ab_role WHERE name='Admin'"
        result1 = self.run_sql(
            1, sql_where, "4", async='true', tmp_table='tmp_async_1', cta='true')
        assert result1['query']['state'] in (
            QueryStatus.PENDING, QueryStatus.RUNNING, QueryStatus.SUCCESS)

        time.sleep(1)

        # Case 1.
        query1 = self.get_query_by_id(result1['query']['serverId'])
        df1 = pd.read_sql_query(query1.select_sql, con=eng)
        self.assertEqual(QueryStatus.SUCCESS, query1.status)
        self.assertEqual([{'name': 'Admin'}], df1.to_dict(orient='records'))
        self.assertEqual(QueryStatus.SUCCESS, query1.status)
        self.assertTrue("SELECT * \nFROM tmp_async_1" in query1.select_sql)
        self.assertTrue("LIMIT 666" in query1.select_sql)
        self.assertEqual(
            "CREATE TABLE tmp_async_1 AS \nSELECT name FROM ab_role "
            "WHERE name='Admin'", query1.executed_sql)
        self.assertEqual(sql_where, query1.sql)
        if eng.name != 'sqlite':
            self.assertEqual(1, query1.rows)
        self.assertEqual(666, query1.limit)
        self.assertEqual(False, query1.limit_used)
        self.assertEqual(True, query1.select_as_cta)
        self.assertEqual(True, query1.select_as_cta_used)

    def test_get_columns_dict(self):
        main_db = db.session.query(models.Database).filter_by(
            database_name='main').first()
        df = main_db.get_df("SELECT * FROM multiformat_time_series", None)
        cdf = dataframe.CaravelDataFrame(df)
        if main_db.sqlalchemy_uri.startswith('sqlite'):
            self.assertEqual(
                [{'is_date': True, 'type': 'datetime_string', 'name': 'ds',
                  'is_dim': False},
                 {'is_date': True, 'type': 'datetime_string', 'name': 'ds2',
                  'is_dim': False},
                 {'agg': 'sum', 'is_date': False, 'type': 'int64',
                  'name': 'epoch_ms', 'is_dim': False},
                 {'agg': 'sum', 'is_date': False, 'type': 'int64',
                  'name': 'epoch_s', 'is_dim': False},
                 {'is_date': True, 'type': 'datetime_string', 'name': 'string0',
                  'is_dim': False},
                 {'is_date': False, 'type': 'object',
                  'name': 'string1', 'is_dim': True},
                 {'is_date': True, 'type': 'datetime_string', 'name': 'string2',
                  'is_dim': False},
                 {'is_date': False, 'type': 'object',
                  'name': 'string3', 'is_dim': True}]
                , cdf.columns_dict
            )
        else:
            self.assertEqual(
                [{'is_date': True, 'type': 'datetime_string', 'name': 'ds',
                  'is_dim': False},
                 {'is_date': True, 'type': 'datetime64[ns]',
                  'name': 'ds2', 'is_dim': False},
                 {'agg': 'sum', 'is_date': False, 'type': 'int64',
                  'name': 'epoch_ms', 'is_dim': False},
                 {'agg': 'sum', 'is_date': False, 'type': 'int64',
                  'name': 'epoch_s', 'is_dim': False},
                 {'is_date': True, 'type': 'datetime_string', 'name': 'string0',
                  'is_dim': False},
                 {'is_date': False, 'type': 'object',
                  'name': 'string1', 'is_dim': True},
                 {'is_date': True, 'type': 'datetime_string', 'name': 'string2',
                  'is_dim': False},
                 {'is_date': False, 'type': 'object',
                  'name': 'string3', 'is_dim': True}]
                , cdf.columns_dict
            )


if __name__ == '__main__':
    unittest.main()
