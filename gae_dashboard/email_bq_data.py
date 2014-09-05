#!/usr/bin/env python

"""Email various analyses -- hard-coded into this file -- from bigquery.

This script will make a bigquery query, possibly massage the data a
bit, and put it into an html table that it emails out.  It can also
graph the data and append the graph to the email as well.

Here's an example of an analysis we could do here:
"Which url-routes took up the most instance hours yesterday?"
"""

import argparse
import collections
import datetime
import email
import email.mime.text
import email.utils
import json
import hashlib
import os
import cPickle
import smtplib
import subprocess


# Report on the previous day by default
_DEFAULT_DAY = datetime.datetime.now() - datetime.timedelta(1)
_DATA_DIRECTORY = os.path.join(os.getenv('HOME'), 'bq_data/')


def _is_number(s):
    """Return True if s is a numeric type or 'looks like' a number."""
    if isinstance(s, (int, long, float)):
        return True
    if isinstance(s, basestring):
        return s.strip('01234567890.-') == ''
    return False


def _pretty_date(yyyymmdd):
    return '%s-%s-%s' % (yyyymmdd[0:4], yyyymmdd[4:6], yyyymmdd[6:8])


def _convert_table_rows_to_lists(table, order):
    """Given a table where rows are dicts, convert them to lists.

    Each dict (each row in the table) must have the same set of keys.
    Those keys are then listed in the list 'order', which lists the
    keys in the order we want them to be in the table.  We then go
    through the table and convert each dict to a list.  Finally, we
    add a row to the top of the table that has the key-names
    (csv-style).
    """
    retval = []
    for row in table:
        row_contents = row.items()
        row_contents.sort(key=lambda (k, v): order.index(k))
        retval.append([v for (k, v) in row_contents])

    retval.insert(0, list(order))
    return retval


def _render_sparkline(data, width=100, height=20):
    """Given a list of values, render a sparkline to a PNG.

    This takes in a list of numbers, and returns the contents of a PNG as a
    string.  A datapoint may be None if it should be omitted.  It will return
    None if there are not enough datapoints to make a plot.  It requires that
    gnuplot be installed.
    """
    existing_data = [datum for datum in data if datum is not None]
    if len(existing_data) < 3:
        return None
    data_lines = []
    for i, datum in enumerate(data):
        if datum is None:
            data_lines.append("")
        else:
            data_lines.append("%s %s" % (i, datum))
    gnuplot_script = """\
unset border
unset xtics
unset ytics
unset key
set lmargin 0
set rmargin 0
set tmargin 0
set bmargin 0
set yrange [%(ymin)s:%(ymax)s]
set xrange [%(xmin)s:%(xmax)s]
set terminal pngcairo size 100,20
plot "-" using 1:2 notitle with lines linetype rgb "black"
%(data)s
e
""" % {
    'data': '\n'.join(data_lines),
    # The bottom of the plot looks better if it has a bit of buffer around the
    # top and bottom data points.  We force the min to be near zero so small
    # increases to something that started out large are viewed in context.
    'ymax': 1.05 * max(existing_data),
    'ymin': -0.05 * max(existing_data),
    # To make the width per time fixed, set the min and max x value so that
    # the plot will include space for any missing data.
    'xmin': 1,
    'xmax': len(data),
}
    gnuplot_proc = subprocess.Popen(['gnuplot'], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE)
    png, _ = gnuplot_proc.communicate(gnuplot_script)
    return png


def _query_bigquery(sql_query):
    """Use the 'bq' tool to run a query, and return the results as
    a json list (each row is a dict).

    We do naive type conversion to int and float, when possible.

    This requires 'pip install bigquery' be run on this machine.
    """
    # We could probably do 'import bq' and call out directly, but
    # I couldn't figure out an easy way to do this.  Ah well.
    # To avoid having to deal with paging (which I think the command-line bq is
    # not very good at anyway), we just get a bunch of rows.  We probably only
    # want to display the first 100 or so, but the rest may be useful to save.
    data = subprocess.check_output(['bq', '-q', '--format=json', '--headless',
                                    'query', '--max_rows=10000', sql_query])
    table = json.loads(data)

    for row in table:
        for key in row:
            if row[key] is None:
                row[key] = '(None)'
            else:
                try:
                    row[key] = int(row[key])
                except ValueError:
                    try:
                        row[key] = float(row[key])
                    except ValueError:
                        pass

    return table


def _send_email(tables, graph, to, cc=None, subject='bq data', preamble=None):
    """Send an email with the given table and graph.

    Arguments:
       tables: a dict, with headings as keys, and values lists of lists of the
           form: [[1A, 1B], [2A, 2B], ...].  Can be None.  If the heading is
           the empty string, it won't be displayed.  If a table cell value is
           itself a list, it will be plotted as a sparkline.
       graph: TODO(csilvers).  Can be None.
       to: a list of email addresses
       cc: an optional list of email addresses
       subject: subject of the email
       preamble: text to put before the table and graph.
    """
    body = []
    if preamble:
        body.append('<p>%s</p>' % preamble)

    if not isinstance(tables, dict):
        tables = {'': tables}

    images = []
    for heading, table in sorted(tables.iteritems()):
        if heading:
            body.append('<h3>%s</h3>' % heading)
        if table and table[0]:
            body.append('<table cellspacing="1" cellpadding="3" border="1"'
                        '       style="table-layout:fixed;font-size:13px;'
                        '              font-family:arial,sans,sans-serif;'
                        '              border-collapse:collapse;border:1px '
                        '              solid rgb(204,204,204)">')
            body.append('<thead>')
            body.append('<tr>')
            for header in table[0]:
                body.append('<th>%s</th>' % header)
            body.append('</tr>')
            body.append('</thead>')
            body.append('<tbody>')
            for row in table[1:]:
                body.append('<tr>')
                for col in row:
                    style = 'padding: 3px 5px 3px 8px;'
                    if isinstance(col, (int, long)):
                        style += 'text-align: right;'
                    elif isinstance(col, float):
                        style += 'text-align: right;'
                        col = '%.2f' % col     # make the output reasonable
                    elif isinstance(col, list):
                        # If we get a list, plot it as a sparkline.
                        style = 'padding: 0px; text-align: center;'
                        # Just put in a placeholder for the datum, we'll fill
                        # in the image in _embed_images_to_mime().
                        image = _render_sparkline(col)
                        if image:
                            images.append(image)
                            col = '%s'
                        else:
                            # If the image didn't render due to insufficient
                            # data, say so rather than leaving it out.
                            col = '(insufficient data)'
                    body.append('<td style="%s">%s</td>' % (style, col))
                body.append('</tr>')
            body.append('</tbody>')
            body.append('</table>')

    if graph:
        pass

    msg = _embed_images_to_mime('\n'.join(body), images)
    msg['Subject'] = subject
    msg['From'] = '"bq-cron-reporter" <toby-admin+bq-cron@khanacademy.org>'
    msg['To'] = ', '.join(to)
    if cc:
        msg['Cc'] = ', '.join(cc)
    s = smtplib.SMTP('localhost')
    s.sendmail('toby-admin+bq-cron@khanacademy.org', to, msg.as_string())
    s.quit()


def _embed_images_to_mime(html, images):
    """Given HTML with placeholders, insert images and return a MIME object.

    "html" should be the HTML body, with %s placeholders for the images.  Other
    % signs must be escaped as %%.  "images" should be a list of PNG images,
    such as those read from a PNG file.  The images will then attached to the
    message, and a MIME object will be returned.
    """
    if not images:
        # For consistency, we should still do a trivial string format to
        # unescape the % signs.  But there's no point jumping through all the
        # hoops of making the multipart MIME.
        return email.MIMEText.MIMEText(html % (), 'html')
    image_tag_template = '<img src="cid:%s" alt=""/>'
    image_tags = []
    image_mimes = []
    for image in images:
        image_id = "%s@khanacademy.org" % hashlib.sha1(image).hexdigest()
        image_tags.append(image_tag_template % image_id)
        image_mime = email.MIMEImage.MIMEImage(image)
        image_mime.add_header('Content-ID', '<%s>' % image_id)
        # Cause the images to only render inline, not as attachments
        # (hopefully; this seems to be a bit buggy in Gmail)
        image_mime.add_header('Content-Disposition', 'inline')
        image_mimes.append(image_mime)
    msg_root = email.MIMEMultipart.MIMEMultipart('related')
    msg_body = email.MIMEText.MIMEText(html % tuple(image_tags), 'html')
    msg_root.attach(msg_body)
    for image_mime in image_mimes:
        msg_root.attach(image_mime)
    return msg_root


def _get_data_filename(report, yyyymmdd):
    """Gets the filename in which old data might be stored.

    Takes a report name and a date stamp.  The file returned is not guaranteed
    to exist.
    """
    return os.path.join(_DATA_DIRECTORY, report + '_' + yyyymmdd + '.pickle')


def _get_past_data(report, yyyymmdd):
    """Gets old data for a particular report.

    Returns the data in the format saved (see _save_daily_data or the caller),
    or None if there is no old data for that report on that day.
    """
    filename = _get_data_filename(report, yyyymmdd)
    if not os.path.exists(filename):
        return None
    else:
        with open(filename) as f:
            return cPickle.load(f)


def _process_past_data(report, end_date, history_length, keyfn):
    """Get and process the past data for a particular report.

    Returns a list of dicts, one for each day, in most-recent-first order, with
    keys of the form returned by keyfn(row), and values the same type of rows
    returned by `bq`.  If there is no data, the dict will be empty.
    'history_length' is the number of days of data to include, not counting the
    current one.
    """
    historical_data = []
    for i in xrange(history_length + 1):
        old_yyyymmdd = (end_date - datetime.timedelta(i)).strftime("%Y%m%d")
        old_data = _get_past_data(report, old_yyyymmdd)
        # Save it by url_route for easy lookup.
        if old_data:
            historical_data.append({keyfn(row): row for row in old_data})
        else:
            # If we're missing data, put in a placeholder.  This will get
            # carried through and eventually become a space in the graph.
            historical_data.append({})
    # We construct historical_data with the most recent data first, but display
    # the sparklines with the most recent data last.
    historical_data.reverse()
    return historical_data


def _save_daily_data(data, report, yyyymmdd):
    """Saves the data for a report to be used in the future.

    This will create the relevant directories if they don't exist, and clobber
    any existing data with the same timestamp.  "data" can be anything
    pickleable, but in general will likely be of the format returned from
    _query_bigquery, namely a list of dicts fieldname -> value.
    """
    filename = _get_data_filename(report, yyyymmdd)
    if not os.path.isdir(os.path.dirname(filename)):
        os.makedirs(os.path.dirname(filename))
    with open(filename, 'w') as f:
        cPickle.dump(data, f)


# This is a map from module-name to how many processors it has -- so
# F4 would be 4, F8 would be 8.  If a module is run with
# multithreading, we divide the num-processes by 8 to emulate having 8
# threads run concurrently.  This is a total approximation!
# TODO(csilvers): use webapp's module_util to get the F-class and
# multithreading status instead of hard-coding.
_MODULE_CPU_COUNT = {
    'default': 4,
    'i18n': 4,
    'frontend-highmem': 4,
    'batch-lowlatency': 2,
    'batch': 2 / 8.0,
    'highmem': 8,
    }


def email_instance_hours(date):
    """Email instance hours report for the given datetime.date object."""
    yyyymmdd = date.strftime("%Y%m%d")
    cost_fn = '\n'.join("WHEN module_id == '%s' THEN latency * %s" % kv
                        for kv in _MODULE_CPU_COUNT.iteritems())
    query = """\
SELECT COUNT(*) as count_,
elog_url_route as url_route,
SUM(CASE %s ELSE 0 END) / 3600 as instance_hours
FROM [logs.requestlogs_%s]
WHERE url_map_entry != "" # omit static files
GROUP BY url_route
ORDER BY instance_hours DESC
""" % (cost_fn, yyyymmdd)
    data = _query_bigquery(query)
    _save_daily_data(data, "instance_hours", yyyymmdd)
    historical_data = _process_past_data(
        "instance_hours", date, 14, lambda row: row['url_route'])

    # Munge the table by adding a few columns.
    total_instance_hours = 0.0
    for row in data:
        total_instance_hours += row['instance_hours']

    for row in data:
        row['%% of total'] = row['instance_hours'] / total_instance_hours * 100
        row['per 1k requests'] = row['instance_hours'] / row['count_'] * 1000
        sparkline_data = []
        for old_data in historical_data:
            old_row = old_data.get(row['url_route'])
            if old_row:
                sparkline_data.append(
                    old_row['instance_hours'] / old_row['count_'])
            else:
                sparkline_data.append(None)
        row['last 2 weeks (per request)'] = sparkline_data

    _ORDER = ('%% of total', 'instance_hours', 'count_', 'per 1k requests',
              'last 2 weeks (per request)', 'url_route')
    data = _convert_table_rows_to_lists(data, _ORDER)

    subject = 'Instance Hours by Route'
    heading = 'Instance hours by route for %s' % _pretty_date(yyyymmdd)
    # Let's just send the top most expensive routes, not all of them.
    _send_email({heading: data[:50]}, None,
                to=['infrastructure-blackhole@khanacademy.org'],
                subject=subject)


def email_rpcs(date):
    """Email RPCs-per-route report for the given datetime.date object."""
    yyyymmdd = date.strftime("%Y%m%d")
    rpc_fields = ('Get', 'Put', 'Next', 'RunQuery', 'Delete')

    inits = ["IFNULL(INTEGER(t%s.rpc_%s), 0) AS rpc_%s" % (name, name, name)
             for name in rpc_fields]
    inits.append("IFNULL(tcost.rpc_cost, 0) AS rpc_cost")
    joins = ["LEFT OUTER JOIN ( "
             "SELECT elog_url_route AS url_route, COUNT(*) AS rpc_%s "
             "FROM FLATTEN([logs.requestlogs_%s], elog_stats_rpc) "
             "WHERE elog_stats_rpc.key = 'stats.rpc.datastore_v3.%s' "
             "GROUP BY url_route) AS t%s ON t1.url_route = t%s.url_route"
             % (name, yyyymmdd, name, name, name)
             for name in rpc_fields]
    joins.append("""\
LEFT OUTER JOIN (
SELECT elog_url_route AS url_route,
       SUM(elog_stats_rpc_ops.value) AS rpc_cost
FROM [logs.requestlogs_%s]
WHERE elog_stats_rpc_ops.key = 'stats.rpc_ops.cost'
GROUP BY url_route) AS tcost ON t1.url_route = tcost.url_route
""" % yyyymmdd)
    query = """\
SELECT t1.url_route AS url_route,
t1.url_requests AS requests,
%s
FROM (
SELECT elog_url_route AS url_route, COUNT(*) AS url_requests
FROM [logs.requestlogs_%s]
GROUP BY url_route) AS t1
%s
ORDER BY tcost.rpc_cost DESC;
""" % (',\n'.join(inits), yyyymmdd, '\n'.join(joins))
    data = _query_bigquery(query)
    _save_daily_data(data, "rpcs", yyyymmdd)
    historical_data = _process_past_data(
        "rpcs", date, 14, lambda row: row['url_route'])

    # Munge the table by getting per-request counts for every RPC stat.
    micropennies = '&mu;&cent;'
    for row in data:
        for stat in rpc_fields:
            row['%s/req' % stat] = row['rpc_%s' % stat] * 1.0 / row['requests']
        row[micropennies + '/req'] = row['rpc_cost'] * 1.0 / row['requests']
        row['$'] = row['rpc_cost'] * 1.0e-8
        sparkline_data = []
        for old_data in historical_data:
            old_row = old_data.get(row['url_route'])
            if old_row and 'rpc_cost' in old_row:
                sparkline_data.append(
                    old_row['rpc_cost'] * 1.0 / old_row['requests'])
            else:
                sparkline_data.append(None)
        row['last 2 weeks (%s/req)' % micropennies] = sparkline_data

        del row['rpc_cost']

    # Convert each row from a dict to a list, in a specific order.
    _ORDER = (['url_route', 'requests', '$', micropennies + '/req',
               'last 2 weeks (%s/req)' % micropennies] +
              ['rpc_%s' % f for f in rpc_fields] +
              ['%s/req' % f for f in rpc_fields])
    data = _convert_table_rows_to_lists(data, _ORDER)

    subject = 'RPC calls by route'
    heading = 'RPC calls by route for %s' % _pretty_date(yyyymmdd)
    # Let's just send the top most expensive routes, not all of them.
    _send_email({heading: data[:75]}, None,
                to=['infrastructure-blackhole@khanacademy.org'],
                subject=subject)


def email_out_of_memory_errors(date):
    # This sends two emails, for two different ways of seeing the data.
    # But we'll have them share the same subject so they thread together.
    yyyymmdd = date.strftime("%Y%m%d")
    subject = 'OOM errors'

    # Out-of-memory errors look like:
    #   Exceeded soft private memory limit with 260.109 MB after servicing 2406 requests total  #@Nolint
    # with SDK 1.9.7, they changed. Note the double-space after "after":
    #   Exceeded soft private memory limit of 512 MB with 515 MB after  servicing 9964 requests total  #@Nolint
    numreqs = r"REGEXP_EXTRACT(app_logs.message, r'servicing (\d+) requests')"
    query = """\
SELECT COUNT(module_id) AS count_,
       module_id,
       NTH(10, QUANTILES(INTEGER(%s), 101)) as numserved_10th,
       NTH(50, QUANTILES(INTEGER(%s), 101)) as numserved_50th,
       NTH(90, QUANTILES(INTEGER(%s), 101)) as numserved_90th
FROM [logs.requestlogs_%s]
WHERE app_logs.message CONTAINS 'Exceeded soft private memory limit'
      AND module_id IS NOT NULL
GROUP BY module_id
ORDER BY count_ DESC
""" % (numreqs, numreqs, numreqs, yyyymmdd)
    data = _query_bigquery(query)
    _save_daily_data(data, "out_of_memory_errors_by_module", yyyymmdd)
    historical_data = _process_past_data(
        "out_of_memory_errors_by_module", date, 14,
        lambda row: row['module_id'])

    for row in data:
        sparkline_data = []
        for old_data in historical_data:
            old_row = old_data.get(row['module_id'])
            if old_row:
                sparkline_data.append(old_row['count_'])
            elif old_data:
                # If we have data, just not on this module, then it just didn't
                # OOM.
                sparkline_data.append(0)
            else:
                # On the other hand, if we don't have data at all, we should
                # show a gap.
                sparkline_data.append(None)
        row['last 2 weeks'] = sparkline_data

    _ORDER = ['count_', 'last 2 weeks', 'module_id',
              'numserved_10th', 'numserved_50th', 'numserved_90th']
    data = _convert_table_rows_to_lists(data, _ORDER)

    heading = 'OOM errors by module for %s' % _pretty_date(yyyymmdd)
    email_content = {heading: data}

    query = """\
SELECT COUNT(*) as count_,
       module_id,
       elog_url_route as url_route
FROM [logs.requestlogs_%s]
WHERE app_logs.message CONTAINS 'Exceeded soft private memory limit'
GROUP BY module_id, url_route
ORDER BY count_ DESC
""" % yyyymmdd
    data = _query_bigquery(query)
    _save_daily_data(data, "out_of_memory_errors_by_route", yyyymmdd)
    historical_data = _process_past_data(
        "out_of_memory_errors_by_route", date, 14,
        lambda row: (row['module_id'], row['url_route']))

    for row in data:
        sparkline_data = []
        for old_data in historical_data:
            old_row = old_data.get((row['module_id'], row['url_route']))
            if old_row:
                sparkline_data.append(old_row['count_'])
            elif old_data:
                # If we have data, just not on this route/module, then it just
                # didn't OOM.
                sparkline_data.append(0)
            else:
                # On the other hand, if we don't have data at all, we should
                # show a gap.
                sparkline_data.append(None)
        row['last 2 weeks'] = sparkline_data

    _ORDER = ['count_', 'last 2 weeks', 'module_id', 'url_route']
    data = _convert_table_rows_to_lists(data, _ORDER)

    heading = 'OOM errors by route for %s' % _pretty_date(yyyymmdd)
    email_content[heading] = data

    _send_email(email_content, None,
                to=['infrastructure-blackhole@khanacademy.org'],
                subject=subject)


def email_memory_increases(date, window_length=20, min_increase_in_mb=1):
    """Emails the increases in memory caused by particular routes.

    It attempts to compute the amount of memory ignoring memory which is
    reclaimed in the next few requests.  (The number of requests which are
    checked is specified by the window_length parameter.  Routes with a total
    increase less than the min_increase_in_mb parameter are ignored.)
    """
    yyyymmdd = date.strftime("%Y%m%d")
    lead_lengths = range(1, window_length + 1)
    lead_selects = '\n'.join(
        "LEAD(total, %s) OVER (PARTITION BY instance_key ORDER BY start_time) "
        "AS lead_total_%s," % (i, i) for i in lead_lengths)

    fields = ['total'] + ['lead_total_%s' % i for i in lead_lengths]
    # We want to compute the minimal value of added + field - total, where
    # field is one of "total" or one of the "lead_total_i".  BigQuery
    # unfortunately doesn't give us a nice way to do this (at least that I know
    # of, without doing a CROSS JOIN of the table to itself).  One way to do
    # this would be a gigantic nested IF, but this could be exponentially
    # large, and queries have a fixed maximum size.  Instead we use a CASE
    # expression which could be O(n^2); since we don't pay for BigQuery
    # execution time, and n shouldn't be too huge, this seems like a better
    # approach.  In theory it might be better to do O(n) nested queries (each
    # of which does a single pairwise min), but this seems like it could in
    # practice be even slower, depending on the implementation.
    # TODO(benkraft): If I get a useful answer to
    # http://stackoverflow.com/questions/24923101/computing-a-moving-maximum-in-bigquery
    # we should use that instead.  Or, once we have user-defined functions in
    # BigQuery, we can probably do something actually reasonable.
    case_expr = '\n'.join(
        "WHEN %s THEN added + %s - total" % (
            ' AND '.join("%s <= %s" % (field1, field2)
                         for field2 in fields if field2 != field1),
            field1)
        for field1 in fields)

    # This is a kind of large query, so here's what it's doing, from inside to
    # out:
    #   First, extract the memory data from the logs.
    #   Second, compute the appropriate LEAD() columns, which tell us what the
    #       total will be a few requests later on the same instance
    #   Third, compute "real_added", which is the amount we think the request
    #       actually added to the heap, not counting memory which was soon
    #       reclaimed.  This might come out negative, so
    #   Fourth, make sure the memory added is at least zero, since if memory
    #       usage went down, this request probably shouldn't get the credit.
    #   Fifth, group by route and do whatever aggregation we want.  Ignore the
    #       first 25 requests to each module, since those are probably all
    #       loading things that each module has to load, and the request that
    #       does the loading shouldn't be blamed.
    query = """\
SELECT
    COUNT(*) AS count_,
    elog_url_route AS url_route,
    module_id AS module,
    AVG(real_added) AS added_avg,
    NTH(99, QUANTILES(real_added, 101)) AS added_98th,
    SUM(real_added) AS added_total,
FROM (
    SELECT
        IF(real_added > 0, real_added, 0) AS real_added,
        elog_url_route, module_id, num,
    FROM (
        SELECT
            (CASE %s ELSE added END) AS real_added,
            elog_url_route, module_id, num,
        FROM (
            SELECT
                %s
                RANK() OVER (PARTITION BY instance_key
                             ORDER BY start_time) AS num,
                added, total, elog_url_route, module_id,
            FROM (
                SELECT
                    FLOAT(REGEXP_EXTRACT(
                        app_logs.message,
                        "This request added (.*) MB to the heap.")) AS added,
                    FLOAT(REGEXP_EXTRACT(
                        app_logs.message,
                        "Total memory now used: (.*) MB")) AS total,
                    instance_key, start_time, elog_url_route, module_id,
                FROM [logs.requestlogs_%s]
                WHERE app_logs.message CONTAINS 'This request added'
            )
        )
    )
)
WHERE num > 25
GROUP BY url_route, module
ORDER BY added_total DESC
""" % (case_expr, lead_selects, yyyymmdd)
    data = _query_bigquery(query)
    _save_daily_data(data, "memory_increases", yyyymmdd)
    historical_data = _process_past_data(
        "memory_increases", date, 14,
        lambda row: (row['module'], row['url_route']))

    by_module = collections.defaultdict(list)
    for row in data:
        if row['added_total'] > min_increase_in_mb:
            heading = "Memory increases by route for %s module on %s" % (
                row['module'], _pretty_date(yyyymmdd))
            by_module[heading].append(row)

    _ORDER = ['count_', 'added_avg', 'last 2 weeks (avg)', 'added_98th',
              'added_total', 'added %%', 'url_route']
    for heading in by_module:
        total = sum(row['added_total'] for row in by_module[heading])
        for row in by_module[heading]:
            row['added %%'] = row['added_total'] / total * 100
            sparkline_data = []
            for old_data in historical_data:
                old_row = old_data.get((row['module'], row['url_route']))
                if old_row:
                    sparkline_data.append(old_row['added_avg'])
                else:
                    sparkline_data.append(None)
            row['last 2 weeks (avg)'] = sparkline_data
            del row['module']
        by_module[heading] = _convert_table_rows_to_lists(
            by_module[heading][:50], _ORDER)
    subject = "Memory Increases by Route"
    _send_email(by_module, None,
                to=['infrastructure-blackhole@khanacademy.org'],
                subject=subject)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', metavar='YYYYMMDD',
                        default=_DEFAULT_DAY.strftime("%Y%m%d"),
                        help=('Date to get reports for, specified as YYYYMMDD '
                              '(default "%(default)s")'))
    args = parser.parse_args()

    date = datetime.datetime.strptime(args.date, "%Y%m%d")

    print 'Emailing instance hour info'
    email_instance_hours(date)

    print 'Emailing rpc stats info'
    email_rpcs(date)

    print 'Emailing out-of-memory info'
    email_out_of_memory_errors(date)

    print 'Emailing memory profiling info'
    email_memory_increases(date)


if __name__ == '__main__':
    main()