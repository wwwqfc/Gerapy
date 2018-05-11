import json
import logging
import os
import sys
import optparse
from scrapy.commands import ScrapyCommand as BaseParser
from scrapy.crawler import CrawlerProcess, CrawlerRunner
from scrapy.utils.project import get_project_settings
from scrapy.settings.deprecated import check_deprecated_settings
from w3lib.url import is_url
from scrapy.utils.conf import arglist_to_dict
from scrapy.exceptions import UsageError
from scrapy.http import Request
from scrapy.item import BaseItem
from scrapy.utils import display
from scrapy.utils.spider import iterate_spider_output, spidercls_for_request
import multiprocessing
from twisted.internet import reactor

logger = logging.getLogger(__name__)

dfs = set()


class SingleParser(BaseParser):
    requires_project = True
    spider = None
    items = {}
    requests = {}
    first_response = None
    
    def short_desc(self):
        return "Parse URL (using its spider) and print the results"
    
    def add_options(self, parser):
        BaseParser.add_options(self, parser)
        parser.add_option("--spider", dest="spider", default=None,
                          help="use this spider without looking for one")
        parser.add_option("-a", dest="spargs", action="append", default=[], metavar="NAME=VALUE",
                          help="set spider argument (may be repeated)")
        parser.add_option("--pipelines", action="store_true",
                          help="process items through pipelines")
        parser.add_option("--nolinks", dest="nolinks", action="store_true",
                          help="don't show links to follow (extracted requests)")
        parser.add_option("--noitems", dest="noitems", action="store_true",
                          help="don't show scraped items")
        parser.add_option("--nocolour", dest="nocolour", action="store_true",
                          help="avoid using pygments to colorize the output")
        parser.add_option("-r", "--rules", dest="rules", action="store_true",
                          help="use CrawlSpider rules to discover the callback")
        parser.add_option("-c", "--callback", dest="callback",
                          help="use this callback for parsing, instead looking for a callback")
        parser.add_option("-m", "--meta", dest="meta",
                          help="inject extra meta into the Request, it must be a valid raw json string")
        parser.add_option("-d", "--depth", dest="depth", type="int", default=1,
                          help="maximum depth for parsing requests [default: %default]")
        parser.add_option("-v", "--verbose", dest="verbose", action="store_true",
                          help="print each depth level one by one")
    
    @property
    def max_level(self):
        levels = list(self.items.keys()) + list(self.requests.keys())
        if not levels:
            return 0
        return max(levels)
    
    def add_items(self, lvl, new_items):
        old_items = self.items.get(lvl, [])
        self.items[lvl] = old_items + new_items
    
    def add_requests(self, lvl, new_reqs):
        old_reqs = self.requests.get(lvl, [])
        self.requests[lvl] = old_reqs + new_reqs
    
    def print_items(self, lvl=None, colour=True):
        if lvl is None:
            items = [item for lst in self.items.values() for item in lst]
        else:
            items = self.items.get(lvl, [])
        
        print("# Scraped Items ", "-" * 60)
        display.pprint([dict(x) for x in items], colorize=colour)
    
    def print_requests(self, lvl=None, colour=True):
        if lvl is None:
            levels = list(self.requests.keys())
            if levels:
                requests = self.requests[max(levels)]
            else:
                requests = []
        else:
            requests = self.requests.get(lvl, [])
        
        print("# Requests ", "-" * 65)
        display.pprint(requests, colorize=colour)
    
    def print_results(self, opts):
        colour = not opts.nocolour
        if opts.verbose:
            for level in range(1, self.max_level + 1):
                print('\n>>> DEPTH LEVEL: %s <<<' % level)
                if not opts.noitems:
                    self.print_items(level, colour)
                if not opts.nolinks:
                    self.print_requests(level, colour)
        else:
            print('\n>>> STATUS DEPTH LEVEL %s <<<' % self.max_level)
            if not opts.noitems:
                self.print_items(colour=colour)
            if not opts.nolinks:
                self.print_requests(colour=colour)
    
    def process_item(self, item):
        return dict(item)
    
    def get_items(self, lvl=None):
        if lvl is None:
            items = [item for lst in self.items.values() for item in lst]
        else:
            items = self.items.get(lvl, [])
        items_array = []
        for item in items:
            items_array.append(self.process_item(item))
        return items_array
    
    def process_request(self, request):
        return {
            'url': request.url,
            'method': request.method,
            'meta': request.meta,
            'headers': request.headers,
            'callback': self.get_callback(request)
        }
    
    def get_requests(self, lvl=None):
        if lvl is None:
            levels = list(self.requests.keys())
            if levels:
                requests = self.requests[max(levels)]
            else:
                requests = []
        else:
            requests = self.requests.get(lvl, [])
        
        requests_array = []
        for request in requests:
            print('Request', request, self.get_callback(request))
            requests_array.append(self.process_request(request))
        
        return requests_array
    
    def process_response(self, response):
        return {
            'html': response.text,
            'url': response.url,
            'status': response.status
        }
    
    def get_response(self):
        return self.process_response(self.first_response)
    
    def get_results(self, opts):
        results = []
        
        for level in range(1, self.max_level + 1):
            if not opts.noitems:
                items = self.get_items(level)
                results += items
            if not opts.nolinks:
                requests = self.get_requests(level)
                results += requests
        return results
    
    def run_callback(self, response, cb):
        items, requests = [], []
        for x in iterate_spider_output(cb(response)):
            if isinstance(x, (BaseItem, dict)):
                items.append(x)
            elif isinstance(x, Request):
                requests.append(x)
        return items, requests
    
    def get_callback(self, request):
        if getattr(self.spidercls, 'rules', None):
            rule_index = request.meta.get('rule', -1)
            if rule_index >= 0 and rule_index < len(self.spidercls.rules):
                rule = self.spidercls.rules[rule_index]
                return rule.callback or 'parse'
            elif request.callback:
                return request.callback.__name__
    
    def get_callback_from_rules(self, spider, response):
        if getattr(spider, 'rules', None):
            for rule in spider.rules:
                if rule.link_extractor.matches(response.url):
                    return rule.callback or "parse"
        else:
            logger.error('No CrawlSpider rules found in spider %(spider)r, '
                         'please specify a callback to use for parsing',
                         {'spider': spider.name})
    
    def set_spidercls(self, url, opts):
        spider_loader = self.crawler_process.spider_loader
        if opts.spider:
            try:
                self.spidercls = spider_loader.load(opts.spider)
            except KeyError:
                logger.error('Unable to find spider: %(spider)s',
                             {'spider': opts.spider})
        else:
            self.spidercls = spidercls_for_request(spider_loader, Request(url))
            if not self.spidercls:
                logger.error('Unable to find spider for: %(url)s',
                             {'url': url})
        
        # Request requires callback argument as callable or None, not string
        request = Request(url, None)
        _start_requests = lambda s: [self.prepare_request(s, request, opts)]
        self.spidercls.start_requests = _start_requests
    
    def start_parsing(self, url, opts):
        self.crawler_process.crawl(self.spidercls, **opts.spargs)
        self.pcrawler = list(self.crawler_process.crawlers)[0]
        # self.crawler_process.start()
        # self.crawler_process.start()
        d = self.crawler_process.join()
        d.addBoth(lambda _: reactor.stop())
        
        reactor.run()
        
        if not self.first_response:
            logger.error('No response downloaded for: %(url)s',
                         {'url': url})
    
    def prepare_request(self, spider, request, opts):
        def callback(response):
            # memorize first request
            if not self.first_response:
                self.first_response = response
            
            # determine real callback
            cb = response.meta['_callback']
            if not cb:
                if opts.callback:
                    cb = opts.callback
                elif opts.rules and self.first_response == response:
                    cb = self.get_callback_from_rules(spider, response)
                    
                    if not cb:
                        logger.error('Cannot find a rule that matches %(url)r in spider: %(spider)s',
                                     {'url': response.url, 'spider': spider.name})
                        return
                else:
                    cb = 'parse'
            
            if not callable(cb):
                cb_method = getattr(spider, cb, None)
                if callable(cb_method):
                    cb = cb_method
                else:
                    logger.error('Cannot find callback %(callback)r in spider: %(spider)s',
                                 {'callback': cb, 'spider': spider.name})
                    return
            
            # parse items and requests
            depth = response.meta['_depth']
            
            items, requests = self.run_callback(response, cb)
            if opts.pipelines:
                itemproc = self.pcrawler.engine.scraper.itemproc
                for item in items:
                    itemproc.process_item(item, spider)
            self.add_items(depth, items)
            self.add_requests(depth, requests)
            
            if depth < opts.depth:
                for req in requests:
                    req.meta['_depth'] = depth + 1
                    req.meta['_callback'] = req.callback
                    req.callback = callback
                return requests
        
        # update request meta if any extra meta was passed through the --meta/-m opts.
        if opts.meta:
            request.meta.update(opts.meta)
        
        request.meta['_depth'] = 1
        request.meta['_callback'] = request.callback
        request.callback = callback
        return request
    
    def process_options(self, args, opts):
        BaseParser.process_options(self, args, opts)
        
        self.process_spider_arguments(opts)
        self.process_request_meta(opts)
    
    def process_spider_arguments(self, opts):
        try:
            opts.spargs = arglist_to_dict(opts.spargs)
        except ValueError:
            raise UsageError("Invalid -a value, use -a NAME=VALUE", print_help=False)
    
    def process_request_meta(self, opts):
        if opts.meta:
            try:
                opts.meta = json.loads(opts.meta)
            except ValueError:
                raise UsageError("Invalid -m/--meta value, pass a valid json string to -m or --meta. " \
                                 "Example: --meta='{\"foo\" : \"bar\"}'", print_help=False)
    
    def run(self, args, opts):
        # parse arguments
        if not len(args) == 1 or not is_url(args[0]):
            raise UsageError()
        else:
            url = args[0]
        
        # prepare spidercls
        self.set_spidercls(url, opts)
        
        if self.spidercls and opts.depth > 0:
            self.start_parsing(url, opts)
            self.print_results(opts)
            results = self.get_results(opts)
            print(len(results))
            return results


def execute(url, project, spider, callback, result):
    argv = sys.argv
    argv.append(url)
    if spider:
        argv.append('--spider')
        argv.append(spider)
    if callback:
        argv.append('--callback')
        argv.append(callback)
    
    work_cwd = os.getcwd()
    try:
        os.chdir(project)
        print('Move to ', project)
        settings = get_project_settings()
        check_deprecated_settings(settings)
        parser = optparse.OptionParser(formatter=optparse.TitledHelpFormatter(), conflict_handler='resolve')
        cmd = SingleParser()
        settings.setdict(cmd.default_settings, priority='command')
        cmd.settings = settings
        cmd.add_options(parser)
        opts, _ = parser.parse_args(args=argv[1:])
        args = [url]
        print('opt, args', opts, args)
        cmd.process_options(args, opts)
        cmd.crawler_process = CrawlerRunner(settings)
        # cmd.crawler_process = CrawlerProcess(settings)
        cmd.run(args, opts)
        requests, items, response = cmd.get_requests(), cmd.get_items(), cmd.get_response()
        result['requests'] = requests
        result['items'] = items
        result['response'] = response
    finally:
        os.chdir(work_cwd)


def run(url, project, spider, callback):
    """
    run parser
    :param url:
    :param project:
    :param spider:
    :param callback:
    :return:
    """
    manager = multiprocessing.Manager()
    result = manager.dict()
    jobs = []
    p = multiprocessing.Process(target=execute, args=(url, project, spider, callback, result))
    jobs.append(p)
    p.start()
    
    for proc in jobs:
        proc.join()
    return dict(result)


if __name__ == '__main__':
    url = 'http://tech.china.com/internet/'
    spider = 'china'
    callback = None
    project = '/Users/CQC/testcase/gerapy/projects/example'
    result = run(url, project, spider, callback)
    print('Result', result)