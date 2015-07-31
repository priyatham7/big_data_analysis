import datetime

import luigi
import luigi.date_interval
import luigi.contrib.spark
from luigi.contrib.spark import SparkSubmitTask

from edx.analytics.tasks.calender import CalendarTableTask
from edx.analytics.tasks.mapreduce import MapReduceJobTask, MapReduceJobTaskMixin
from edx.analytics.tasks.pathutil import EventLogSelectionMixin, EventLogSelectionDownstreamMixin
from edx.analytics.tasks.url import get_target_from_url,url_path_join
import edx.analytics.tasks.util.eventlog as eventlog
from edx.analytics.tasks.util import Week
from edx.analytics.tasks.util.hive import WarehouseMixin, HiveTableTask, HivePartition, HiveQueryToMysqlTask

from pyspark import SparkContext
from pyspark.sql import HiveContext

ACTIVE_LABEL = "ACTIVE"
PROBLEM_LABEL = "ATTEMPTED_PROBLEM"
PLAY_VIDEO_LABEL = "PLAYED_VIDEO"
POST_FORUM_LABEL = "POSTED_FORUM"

class UserActivityMixin(object):
	
	interval=luigi.DateIntervalParameter()

class UserActivityTask(luigi.Task):
    """
    Categorize activity of users.
    Analyze the history of user actions and categorize their activity. Note that categories are not mutually exclusive.
    A single event may belong to multiple categories. For example, we define a generic "ACTIVE" category that refers
    to any event that has a course_id associated with it, but is not an enrollment event. Other events, such as a
    video play event, will also belong to other categories.
    The output from this job is a table that represents the number of events seen for each user in each course in each
    category on each day.
    Parameters:
        output_root (str): path to store the output in.

	Username is actually the lmsUserId
    """

    output_root = luigi.Parameter()
    def output(self):
        return get_target_from_url(url_path_join(self.output_root,'tester'))

    
    def mapp(self, course_id, username, date_string, eventName):
	for label in self.get_predicate_labels(eventName):
            yield self._encode_tuple((course_id, username, date_string, label)), 1

    def get_predicate_labels(self, event):
        """Creates labels by applying hardcoded predicates to a single event."""

        labels = [ACTIVE_LABEL]

    	if event == 'problem_check':
		labels.append(PROBLEM_LABEL)

        if event == 'thread_create':
                labels.append(POST_FORUM_LABEL)

        if event == 'play_video':
                labels.append(PLAY_VIDEO_LABEL)

        return labels

    def _encode_tuple(self, values):
        """
        Convert values into a tuple containing encoded strings.
        Parameters:
            Values is a list or tuple.
        This enforces a standard encoding for the parts of the
        key. Without this a part of the key might appear differently
        in the key string when it is coerced to a string by luigi. For
        example, if the same key value appears in two different
        records, one as a str() type and the other a unicode() then
        without this change they would appear as u'Foo' and 'Foo' in
        the final key string. Although python doesn't care about this
        difference, hadoop does, and will bucket the values
        separately. Which is not what we want.
        """
        # TODO: refactor this into a utility function and update jobs
        # to always UTF8 encode mapper keys.
        if len(values) > 1:
            return tuple([value.encode('utf8') for value in values])
        else:
            return values[0].encode('utf8')

    def run(self):
	sc = SparkContext("local", "Course Activity")
	#sqlHC is the SQLHiveContext        
	sqlHC = HiveContext(sc)
	
	lines=sqlHC.sql(""" select courseName,lmsUserId,createDateTime,
		            eventType,eventName,eventNo from logdata where 
			    eventType not in ('enrollment','instructor','admin') 
			    and lmsUserId is not NULL 
   			    and courseName is not NULL 
			    and eventNo is not NULL limit 10""")


	maplvl1=lines.flatMap(lambda p: mapp(p[0],str(p[1]),p[2].strftime('%Y-%m-%d'),p[4]))
	reduceRDD=maplvl1.reduceByKey(lambda a,b : a+b)
	with self.output().open('w') as out_file:
		for line in reduceRDD.collect():
        		out_file.write(line[0][0]+"\x01"+line[0][1]+"\x01"+line[0][2]+"\x01"+line[0][3]+"\x01"+str(line[1])+"\n")
	
class UserActivityTableTask(UserActivityMixin,HiveTableTask):
    """Hive table that stores the set of users active in each course over time."""

    @property
    def table(self):
        return 'user_activity_daily'

    @property
    def columns(self):
        return [
            ('course_id', 'STRING'),
            ('username', 'STRING'),
            ('date', 'STRING'),
            ('category', 'STRING'),
            ('count', 'INT'),
        ]

    @property
    def partition(self):
        return HivePartition('dt',self.interval.date_b.isoformat())  # pylint: disable=no-member

    def requires(self):
        return UserActivityTask(
            output_root=self.partition_location,
        )

class CourseActivityTask(UserActivityMixin,HiveQueryToMysqlTask):
    """Base class for activity queries, captures common dependencies and parameters."""

    @property
    def query(self):
        return self.activity_query.format(
            interval_start=self.interval.date_a.isoformat(),
            interval_end=self.interval.date_b.isoformat(),
        )

    @property
    def activity_query(self):
        raise NotImplementedError

    @property
    def partition(self):
        return HivePartition('dt', self.interval.date_b.isoformat())  # pylint: disable=no-member

    @property
    def required_table_tasks(self):
        yield (
            UserActivityTableTask(
                interval=self.interval,
                warehouse_path=self.warehouse_path,
            ),
            CalendarTableTask(
                warehouse_path=self.warehouse_path,
                overwrite=self.hive_overwrite,
            )
        )

class CourseActivityWeeklyTask(CourseActivityTask):
    """
    Number of users performing each category of activity each ISO week.
    Note that this was the original activity metric, so it is stored in the original table that is simply named
    "course_activity" even though it should probably be named "course_activity_weekly". Also the schema does not match
    the other activity tables for the same reason.
    All references to weeks in here refer to ISO weeks. Note that ISO weeks may belong to different ISO years than the
    Gregorian calendar year.
    If, for example, you wanted to analyze all data in the past week, you could run the job on Monday and pass in 1 to
    the "weeks" parameter. This will not analyze data for the week that contains the current day (since it is not
    complete). It will only compute data for the previous week.
    TODO: update table name and schema to be consistent with other tables.
    Parameters:
        end_date (date): A day within the upper bound week. The week that contains this date will *not* be included in
            the analysis, however, all of the data up to the first day of this week will be included. This is consistent
            with all of our existing closed-open intervals.
        weeks (int): The number of weeks to include in the analysis, counting back from the week that contains the
            end_date.
    """

    end_date = luigi.DateParameter(default=datetime.datetime.utcnow().date())
    weeks = luigi.IntParameter(default=24)

    @property
    def interval(self):
        """Given the parameters, compute the first and last date of the interval."""

        if self.weeks == 0:
            raise ValueError('Number of weeks to process must be greater than 0')

        starting_week = self.get_iso_week_containing_date(self.end_date - datetime.timedelta(weeks=self.weeks))
        ending_week = self.get_iso_week_containing_date(self.end_date)

        # include all complete weeks up to but not including the week containing the end_date
        return luigi.date_interval.Custom(starting_week.monday(), ending_week.monday())

    def get_iso_week_containing_date(self, date):
        iso_year, iso_weekofyear, iso_weekday = date.isocalendar()
        return Week(iso_year, iso_weekofyear)

    @property
    def table(self):
        return 'course_activity'

    @property
    def activity_query(self):
        # Note that hive timestamp format is "yyyy-mm-dd HH:MM:SS.ffff" so we have to snap all of our dates to midnight
        return """
            SELECT
                act.course_id as course_id,
                CONCAT(cal.iso_week_start, ' 00:00:00') as interval_start,
                CONCAT(cal.iso_week_end, ' 00:00:00') as interval_end,
                act.category as label,
                COUNT(DISTINCT username) as count
            FROM user_activity_daily act
            JOIN calendar cal ON act.date = cal.date
            WHERE "{interval_start}" <= cal.date AND cal.date < "{interval_end}"
            GROUP BY
                act.course_id,
                cal.iso_week_start,
                cal.iso_week_end,
                act.category;
        """

    @property
    def columns(self):
        return [
            ('course_id', 'VARCHAR(255) NOT NULL'),
            ('interval_start', 'DATETIME NOT NULL'),
            ('interval_end', 'DATETIME NOT NULL'),
            ('label', 'VARCHAR(255) NOT NULL'),
            ('count', 'INT(11) NOT NULL'),
        ]

    @property
    def indexes(self):
        return [
            ('course_id', 'label'),
            ('interval_end',)
        ]


class CourseActivityDailyTask(CourseActivityTask):
    """Number of users performing each category of activity each calendar day."""

    @property
    def table(self):
        return 'course_activity_daily'

    @property
    def activity_query(self):
        return """
            SELECT
                act.date,
                act.course_id as course_id,
                act.category as label,
                COUNT(DISTINCT username) as count
            FROM user_activity_daily act
            WHERE "{interval_start}" <= act.date AND act.date < "{interval_end}"
            GROUP BY
                act.course_id,
                act.date,
                act.category;
        """

    @property
    def columns(self):
        return [
            ('date', 'DATE NOT NULL'),
            ('course_id', 'VARCHAR(255) NOT NULL'),
            ('label', 'VARCHAR(255) NOT NULL'),
            ('count', 'INT(11) NOT NULL'),
        ]

    @property
    def indexes(self):
        return [
            ('course_id', 'label'),
            ('date',)
        ]


class CourseActivityMonthlyTask(CourseActivityTask):
    """
    Number of users performing each category of activity each calendar month.
    Note that the month containing the end_date is not included in the analysis.
    If, for example, you wanted to analyze all data in the past month, you could run the job on the first day of the
    following month pass in 1 to the "months" parameter. This will not analyze data for the month that contains the
    current day (since it is not complete). It will only compute data for the previous month.
    Parameters:
        end_date (date): A date within the month that will be the upper bound of the closed-open interval.
        months (int): The number of months to include in the analysis, counting back from the month that contains the
            end_date.
    """

    end_date = luigi.DateParameter(default=datetime.datetime.utcnow().date())
    months = luigi.IntParameter(default=6)

    @property
    def interval(self):
        """Given the parameters, compute the first and last date of the interval."""
        from dateutil.relativedelta import relativedelta

        # We don't actually care about the particular day of the month in this computation since we are fixing both the
        # start and end dates to the first day of the month, so we can perform simple arithmetic with the numeric month
        # and only have to worry about adjusting the year. Note that bankers perform this arithmetic differently so it
        # is spelled out here explicitly even though their are third party libraries that contain this computation.

        if self.months == 0:
            raise ValueError('Number of months to process must be greater than 0')

        ending_date = self.end_date.replace(day=1)
        starting_date = ending_date - relativedelta(months=self.months)

        return luigi.date_interval.Custom(starting_date, ending_date)

    @property
    def table(self):
        return 'course_activity_monthly'

    @property
    def activity_query(self):
        return """
            SELECT
                act.course_id as course_id,
                cal.year,
                cal.month,
                act.category as label,
                COUNT(DISTINCT username) as count
            FROM user_activity_daily act
            JOIN calendar cal ON act.date = cal.date
            WHERE "{interval_start}" <= cal.date AND cal.date < "{interval_end}"
            GROUP BY
                act.course_id,
                cal.year,
                cal.month,
                act.category;
        """

    @property
    def columns(self):
        return [
            ('course_id', 'VARCHAR(255) NOT NULL'),
            ('year', 'INT(11) NOT NULL'),
            ('month', 'INT(11) NOT NULL'),
            ('label', 'VARCHAR(255) NOT NULL'),
            ('count', 'INT(11) NOT NULL'),
        ]

    @property
    def indexes(self):
        return [
            ('course_id', 'label'),
            ('year', 'month')
        ]


class GenerateAllDatabaseTablesTask(UserActivityMixin, luigi.WrapperTask):
    """Imports a set of database tables from an external LMS RDBMS."""
    def requires(self):
        kwargs = {
            'interval': self.interval,
        }
        yield (
	    UserActivityTableTask(**kwargs),
        )

    def output(self):
        return [task.output() for task in self.requires()]

if __name__ == '__main__':
    luigi.run()
