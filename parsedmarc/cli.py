#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""A CLI for parsing DMARC reports"""

from argparse import Namespace, ArgumentParser
from configparser import ConfigParser
from glob import glob
import logging
from collections import OrderedDict
import json
from ssl import CERT_NONE, create_default_context

from parsedmarc import IMAPError, get_dmarc_reports_from_inbox, \
    parse_report_file, elastic, kafkaclient, splunk, save_output, \
    watch_inbox, email_results, SMTPError, ParserError, __version__

logger = logging.getLogger("parsedmarc")


def _str_to_list(s):
    """Converts a comma separated string to a list"""
    _list = s.split(",")
    return list(map(lambda i: i.lstrip(), _list))


def _main():
    """Called when the module is executed"""
    def process_reports(reports_):
        output_str = "{0}\n".format(json.dumps(reports_,
                                               ensure_ascii=False,
                                               indent=2))
        if not opts.silent:
            print(output_str)
        if opts.kafka_hosts:
            try:
                kafka_client = kafkaclient.KafkaClient(
                    opts.kafka_hosts,
                    username=opts.kafka_username,
                    password=opts.kafka_password
                )
            except Exception as error_:
                logger.error("Kafka Error: {0}".format(error_.__str__()))
        if opts.save_aggregate:
            for report in reports_["aggregate_reports"]:
                try:
                    if opts.elasticsearch_hostss:
                        elastic.save_aggregate_report_to_elasticsearch(
                            report,
                            index_suffix=opts.elasticsearch_index_suffix,
                            monthly_indexes=opts.elasticsearch_monthly_indexes)
                except elastic.AlreadySaved as warning:
                    logger.warning(warning.__str__())
                except elastic.ElasticsearchError as error_:
                    logger.error("Elasticsearch Error: {0}".format(
                        error_.__str__()))
                try:
                    if opts.kafka_hosts:
                        kafka_client.save_aggregate_reports_to_kafka(
                            report, kafka_aggregate_topic)
                except Exception as error_:
                    logger.error("Kafka Error: {0}".format(
                         error_.__str__()))
            if opts.hec:
                try:
                    aggregate_reports_ = reports_["aggregate_reports"]
                    if len(aggregate_reports_) > 0:
                        hec_client.save_aggregate_reports_to_splunk(
                            aggregate_reports_)
                except splunk.SplunkError as e:
                    logger.error("Splunk HEC error: {0}".format(e.__str__()))
        if opts.save_forensic:
            for report in reports_["forensic_reports"]:
                try:
                    if opts.elasticsearch_hostss:
                        elastic.save_forensic_report_to_elasticsearch(
                            report,
                            index_suffix=opts.elasticsearch_index_suffix,
                            monthly_indexes=opts.elasticsearch_monthly_indexes)
                except elastic.AlreadySaved as warning:
                    logger.warning(warning.__str__())
                except elastic.ElasticsearchError as error_:
                    logger.error("Elasticsearch Error: {0}".format(
                        error_.__str__()))
                try:
                    if opts.kafka_hosts:
                        kafka_client.save_forensic_reports_to_kafka(
                            report, kafka_forensic_topic)
                except Exception as error_:
                    logger.error("Kafka Error: {0}".format(
                        error_.__str__()))
            if opts.hec:
                try:
                    forensic_reports_ = reports_["forensic_reports"]
                    if len(forensic_reports_) > 0:
                        hec_client.save_forensic_reports_to_splunk(
                            forensic_reports_)
                except splunk.SplunkError as e:
                    logger.error("Splunk HEC error: {0}".format(e.__str__()))

    arg_parser = ArgumentParser(description="Parses DMARC reports")
    arg_parser.add_argument("-c", "--config-file",
                            help="A path to a configuration file "
                                 "(--silent implied)")
    arg_parser.add_argument("file_path", nargs="*",
                            help="one or more paths to aggregate or forensic "
                                 "report files or emails")
    strip_attachment_help = "remove attachment payloads from forensic " \
                            "report output"
    arg_parser.add_argument("--strip-attachment-payloads",
                            help=strip_attachment_help, action="store_true")
    arg_parser.add_argument("-o", "--output",
                            help="write output files to the given directory")
    arg_parser.add_argument("-n", "--nameservers", nargs="+",
                            help="nameservers to query "
                                 "(default is Cloudflare's nameservers)")
    arg_parser.add_argument("-t", "--dns_timeout",
                            help="number of seconds to wait for an answer "
                                 "from DNS (default: 6.0)",
                            type=float,
                            default=6.0)
    arg_parser.add_argument("-s", "--silent", action="store_true",
                            help="only print errors and warnings")
    arg_parser.add_argument("--debug", action="store_true",
                            help="print debugging information")
    arg_parser.add_argument("--log-file", default=None,
                            help="output logging to a file")
    arg_parser.add_argument("-v", "--version", action="version",
                            version=__version__)

    aggregate_reports = []
    forensic_reports = []

    args = arg_parser.parse_args()
    opts = Namespace(file_path=args.file_path,
                     onfig_file=args.config_file,
                     strip_attachment_payloads=args.strip_attachment_payloads,
                     output=args.output,
                     nameservers=args.nameservers,
                     silent=args.silent,
                     dns_timeout=args.dns_timeout,
                     debug=args.debug,
                     save_aggregate=False,
                     save_forensic=False,
                     imap_host=None,
                     imap_skip_certificate_verification=False,
                     imap_ssl=True,
                     imap_port=993,
                     imap_user=None,
                     imap_password=None,
                     imap_reports_folder="INBOX",
                     imap_archive_folder="Archive",
                     imap_watch=False,
                     imap_delete=False,
                     imap_test=False,
                     hec=None,
                     hec_token=None,
                     hec_index=None,
                     hec_skip_certificate_verification=False,
                     elasticsearch_hostss=None,
                     elasticsearch_index_suffix=None,
                     elasticsearch_ssl=True,
                     kafka_hosts=None,
                     kafka_username=None,
                     kafka_password=None,
                     kafka_aggregate_topic=None,
                     kafka_forensic_topic=None,
                     kafka_ssl=False,
                     smtp_host=None,
                     smtp_port=25,
                     smtp_ssl=False,
                     smtp_user=None,
                     smtp_password=None,
                     smtp_from=None,
                     smtp_to=[],
                     smtp_subject="parsedmarc report",
                     smtp_message="Please see the attached DMARC results.",
                     log_file=args.log_file
                     )
    args = arg_parser.parse_args()

    if args.config_file:
        opts.silent = True
        config = ConfigParser()
        config.read(args.config_file)
        if "general" in config.sections():
            general_config = config["general"]
            if "strip_attachments_payloads" in general_config:
                opts.strip_attachment_payloads = general_config[
                    "strip_attachment_payloads"]
            if "output" in general_config:
                opts.output = general_config["output"]
            if "nameservers" in general_config:
                opts.nameservers = _str_to_list(general_config["nameservers"])
            if "dns_timeout" in general_config:
                opts.dns_timeout = general_config.getfloat("dns_timeout")
            if "save_aggregate" in general_config:
                opts.save_aggregate = general_config["save_aggregate"]
            if "save_forensic" in general_config:
                opts.save_forensic = general_config["save_forensic"]
            if "debug" in general_config:
                opts.debug = general_config.getboolean("debug")
            if "silent" in general_config:
                opts.silent = general_config.getboolean("silent")
            if "log_file" in general_config:
                opts.log_file = general_config["log_file"]
        if "imap" in config.sections():
            imap_config = config["imap"]
            if "host" in imap_config:
                opts.imap_host = imap_config["host"]
            if "port" in imap_config:
                opts.imap_port = imap_config["port"]
            if "ssl" in imap_config:
                opts.imap_ssl = imap_config.getboolean("ssl")
            if "skip_certificate_verification" in imap_config:
                imap_verify = imap_config.getboolean(
                    "skip_certificate_verification")
                opts.imap_skip_certificate_verification = imap_verify
            if "user" in imap_config:
                opts.imap_user = imap_config["user"]
            if "password" in imap_config:
                opts.imap_password = imap_config["password"]
            if "reports_folder" in imap_config:
                opts.imap_reports_folder = imap_config["reports_folder"]
            if "archive_folder" in imap_config:
                opts.imap_archive_folder = imap_config["archive_folder"]
            if "watch" in imap_config:
                opts.imap_watch = imap_config.getboolean("watch")
            if "delete" in imap_config:
                opts.imap_delete = imap_config.getboolean("delete")
            if "test" in imap_config:
                opts.imap_test = imap_config.getboolean("test")
        if "elasticsearch" in config:
            elasticsearch_config = config["elasticsearch"]
            if "hosts" in elasticsearch_config:
                opts.elasticsearch_urls = _str_to_list(elasticsearch_config[
                    "hosts"])
            if "index_suffix" in elasticsearch_config:
                opts.elasticsearch_index_suffix = elasticsearch_config[
                    "index_suffix"]
            if "monthly_indexes" in elasticsearch_config:
                opts.elasticsearch_monthly_indexes = elasticsearch_config[
                    "monthly_indexes"]
            if "ssl" in elasticsearch_config:
                opts.elasticsearch_ssl = elasticsearch_config.getboolean(
                    "ssl")
            if "cert_path" in elasticsearch_config:
                opts.elasticsearch_ssl_cert_path = elasticsearch_config[
                    "cert_path"]
        if "splunk_hec" in config.sections():
            hec_config = config["splunk_hec"]
            if "url" in hec_config:
                opts.hec = hec_config["url"]
            if "token" in hec_config:
                opts.hec_token = hec_config["token"]
            if "index" in hec_config:
                opts.hec_index = hec_config["index"]
            if "skip_certificate_verification" in hec_config:
                opts.hec_skip_certificate_verification = hec_config[
                    "skip_certificate_verification"]
        if "kafka" in config.sections():
            kafka_config = config["kafka"]
            if "hosts" in kafka_config:
                opts.kafka_hosts = _str_to_list(kafka_config["hosts"])
            if "user" in kafka_config:
                opts.kafka_username = kafka_config["user"]
            if "password" in kafka_config:
                opts.kafka_password = kafka_config["password"]
            if "ssl" in kafka_config:
                opts.kafka_ssl = kafka_config["ssl"].getboolean()
            if "aggregate_topic" in kafka_config:
                opts.kafka_aggregate = kafka_config["aggregate_topic"]
            if "forensic_topic" in kafka_config:
                opts.kafka_username = kafka_config["forensic_topic"]
        if "smtp" in config.sections():
            smtp_config = config["smtp"]
            if "host" in smtp_config:
                opts.smtp_host = smtp_config["host"]
            if "port" in smtp_config:
                opts.smtp_port = smtp_config["port"]
            if "ssl" in smtp_config:
                opts.smtp_ssl = smtp_config.getboolean("ssl")
            if "user" in smtp_config:
                opts.smtp_user = smtp_config["user"]
            if "password" in smtp_config:
                opts.smtp_password = smtp_config["password"]
            if "from" in smtp_config:
                opts.smtp_from = smtp_config["from"]
            if "to" in smtp_config:
                opts.smtp_to = _str_to_list(smtp_config["to"])
            if "subject" in smtp_config:
                opts.smtp_subject = smtp_config["subject"]
            if "attachment" in smtp_config:
                opts.smtp_attachment = smtp_config["attachment"]
            if "message" in smtp_config:
                opts.smtp_message = smtp_config["message"]

    logging.basicConfig(level=logging.WARNING)
    logger.setLevel(logging.WARNING)

    if opts.debug:
        logging.basicConfig(level=logging.DEBUG)
        logger.setLevel(logging.DEBUG)
    if opts.log_file:
        fh = logging.FileHandler(opts.log_file)
        formatter = logging.Formatter(
            '%(asctime)s - '
            '%(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    if opts.imap_host is None and len(opts.file_path) == 0:
        logger.error("You must supply input files, or an IMAP configuration")
        exit(1)

    if opts.save_aggregate or opts.save_forensic:
        try:
            if opts.elasticsearch_hostss:
                es_aggregate_index = "dmarc_aggregate"
                es_forensic_index = "dmarc_forensic"
                if opts.elasticsearch_index_suffix:
                    suffix = opts.elasticsearch_index_suffix
                    es_aggregate_index = "{0}_{1}".format(
                        es_aggregate_index, suffix)
                    es_forensic_index = "{0}_{1}".format(
                        es_forensic_index, suffix)
                elastic.set_hosts(opts.elasticsearch_hostss,
                                  opts.elasticsearch_ssl,
                                  opts.elasticsearch_ssl_cert_path)
                elastic.migrate_indexes(aggregate_indexes=[es_aggregate_index],
                                        forensic_indexes=[es_forensic_index])
        except elastic.ElasticsearchError as error:
            logger.error("Elasticsearch Error: {0}".format(error.__str__()))
            exit(1)

    if opts.hec:
        if opts.hec_token is None or opts.hec_index is None:
            logger.error("HEC token and HEC index are required when "
                         "using HEC URL")
            exit(1)

        verify = True
        if opts.hec_skip_certificate_verification:
            verify = False
        hec_client = splunk.HECClient(opts.hec, opts.hec_token,
                                      opts.hec_index,
                                      verify=verify)

    kafka_aggregate_topic = opts.kafka_aggregate_topic
    kafka_forensic_topic = opts.kafka_forensic_topic

    file_paths = []
    for file_path in args.file_path:
        file_paths += glob(file_path)
    file_paths = list(set(file_paths))

    for file_path in file_paths:
        try:
            sa = opts.strip_attachment_payloads
            file_results = parse_report_file(file_path,
                                             nameservers=opts.nameservers,
                                             dns_timeout=opts.dns_timeout,
                                             strip_attachment_payloads=sa)
            if file_results["report_type"] == "aggregate":
                aggregate_reports.append(file_results["report"])
            elif file_results["report_type"] == "forensic":
                forensic_reports.append(file_results["report"])

        except ParserError as error:
            logger.error("Failed to parse {0} - {1}".format(file_path,
                                                            error))

    if opts.imap_host:
        try:
            if opts.imap_user is None or opts.imap_password is None:
                logger.error("IMAP user and password must be specified if"
                             "host is specified")

            rf = opts.imap_reports_folder
            af = opts.imap_archive_folder
            ns = opts.nameservers
            sa = opts.strip_attachment_payloads
            ssl = True
            ssl_context = None
            if opts.imap_skip_certificate_verification:
                logger.debug("Skipping IMAP certificate verification")
                ssl_context = create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = CERT_NONE
            if opts.imap_ssl is False:
                ssl = False
            reports = get_dmarc_reports_from_inbox(host=opts.imap_host,
                                                   port=opts.imap_port,
                                                   ssl=ssl,
                                                   ssl_context=ssl_context,
                                                   user=opts.imap_user,
                                                   password=opts.imap_password,
                                                   reports_folder=rf,
                                                   archive_folder=af,
                                                   delete=opts.imap_delete,
                                                   nameservers=ns,
                                                   test=opts.imap_test,
                                                   strip_attachment_payloads=sa
                                                   )

            aggregate_reports += reports["aggregate_reports"]
            forensic_reports += reports["forensic_reports"]

        except IMAPError as error:
            logger.error("IMAP Error: {0}".format(error.__str__()))
            exit(1)

    results = OrderedDict([("aggregate_reports", aggregate_reports),
                           ("forensic_reports", forensic_reports)])

    if opts.output:
        save_output(results, output_directory=opts.output)

    process_reports(results)

    if opts.smtp_host:
        if opts.smtp_from is None or opts.smtp_to is None:
            logger.error("Missing mail from and/or mail to")
            exit(1)

        try:
            email_results(results, opts.smtp_host, opts.smtp_from,
                          opts.smtp_to, ssl=opts.smtp_ssl,
                          user=opts.smtp_user,
                          password=opts.smtp_password,
                          subject=opts.smtp_subject)
        except SMTPError as error:
            logger.error("SMTP Error: {0}".format(error.__str__()))
            exit(1)

    if opts.imap_host and opts.imap_watch:
        logger.info("Watching for email - Quit with ctrl-c")
        ssl = True
        ssl_context = None
        if opts.imap_skip_certificate_verification:
            logger.debug("Skipping IMAP certificate verification")
            ssl_context = create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = CERT_NONE
        if opts.imap_ssl is False:
            ssl = False
        try:
            sa = opts.strip_attachment_payloads
            watch_inbox(opts.imap_host, opts.imap_user, opts.imap_password,
                        process_reports, port=opts.imap_port, ssl=ssl,
                        ssl_context=ssl_context,
                        reports_folder=opts.imap_reports_folder,
                        archive_folder=opts.imap_archive_folder,
                        delete=opts.imap_delete,
                        test=opts.imap_test, nameservers=opts.nameservers,
                        dns_timeout=opts.dns_timeout,
                        strip_attachment_payloads=sa)
        except IMAPError as error:
            logger.error("IMAP error: {0}".format(error.__str__()))
            exit(1)


if __name__ == "__main__":
    _main()
