# -*- coding: utf-8 -*-
#!/usr/bin/env python
#
# Copyright 2012 BigML
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""BigMLer - A Higher Level API to BigML's API

# Basic usage
python bigmler.py \
    --train data/iris.csv \
    --test data/test_iris.csv
    --no-test-header

# Create an 10-model ensemble using bagging
python bigmler.py
    --train train.csv \
    --output submission.csv \
    --objective 0 \
    --types types.txt \
    --name 'Iris Ensemble' \
    --number_of_models 10 \
    --sample_rate 0.75 \
    --replacement \
    --tag my_ensemble

# Make predictions using models tagged with my_ensemble
python bigmler.py \
    --model_tag my_ensemble \
    --test test.csv
    --no-test-header

"""
from __future__ import absolute_import

import sys
import os
import datetime
import csv
import re
import shlex

try:
    import simplejson as json
except ImportError:
    import json

import bigml.api
from bigml.model import Model
from bigml.multimodel import MultiModel
from bigml.multivote import COMBINATION_WEIGHTS, COMBINER_MAP, PLURALITY
from bigml.fields import Fields

from bigml.util import localize, console_log, get_csv_delimiter, \
    get_predictions_file_name

from bigmler.options import create_parser
from bigmler.defaults import get_user_defaults
from bigmler.defaults import DEFAULTS_FILE
from bigmler.utils import read_description, read_field_attributes, \
    read_types, read_models, read_dataset, read_json_filter, \
    read_lisp_filter, read_votes_files, list_source_ids, list_dataset_ids, \
    list_model_ids, list_prediction_ids, combine_votes, delete, check_dir, \
    write_prediction, get_log_reversed, is_source_created, checkpoint, \
    is_dataset_created, are_models_created, are_predictions_created, \
    file_number_of_lines, is_evaluation_created, list_evaluation_ids, \
    get_date, prediction_to_row, read_fields_map, print_tree, get_url, \
    log_message

from bigmler.utils import NEW_DIRS_LOG

MAX_MODELS = 10
EVALUATE_SAMPLE_RATE = 0.8
SEED = "BigML, Machine Learning made easy"
# Date and time in format SunNov0412_120510 to name and tag resources
NOW = datetime.datetime.now().strftime("%a%b%d%y_%H%M%S")
COMMAND_LOG = ".bigmler"
DIRS_LOG = ".bigmler_dir_stack"
SESSIONS_LOG = ".bigmler_sessions"
LOG_FILES = [COMMAND_LOG, DIRS_LOG, NEW_DIRS_LOG]


def predict(test_set, test_set_header, models, fields, output,
            objective_field, remote=False, api=None, log=None,
            max_models=MAX_MODELS, method=0, resume=False,
            tags=None, verbosity=1, session_file=None):
    """Computes a prediction for each entry in the `test_set`.

       Predictions can be computed remotely, locally using MultiModels built
       on all the models or locally using MultiModels on subgroups of models
       to limit memory usage and save each model predictions for further use.
    """

    def draw_progress_bar(current, total):
        """Draws a text based progress report.

        """
        pct = 100 - ((total - current) * 100) / (total)
        console_log("Predicted on %s out of %s models [%s%%]" % (
            localize(current), localize(total), pct))

    try:
        test_reader = csv.reader(open(test_set, "U"),
                                 delimiter=get_csv_delimiter(),
                                 lineterminator="\n")
    except IOError:
        sys.exit("Error: cannot read test %s" % test_set)

    headers = None
    exclude = []
    if test_set_header:
        headers = test_reader.next()
        # validate headers against model fields excluding objective_field,
        # that may be present or not
        fields_names = [fields.fields[fields.field_id(i)]
                        ['name'] for i in
                        sorted(fields.fields_by_column_number.keys())
                        if i != fields.field_column_number(objective_field)]
        headers = [unicode(header, "utf-8") for header in headers]
        exclude = [i for i in range(len(headers)) if not headers[i]
                   in fields_names]
        exclude.reverse()
        if len(exclude):
            if (len(headers) - len(exclude)):
                print (u"WARNING: predictions will be processed but some data"
                       u" might not be used. The used fields will be:\n\n%s"
                       u"\n\nwhile the headers found in the test file are:"
                       u"\n\n%s" %
                       (",".join(fields_names),
                        ",".join(headers))).encode("utf-8")
                for index in exclude:
                    del headers[index]
            else:
                raise Exception((u"No test field matches the model fields.\n"
                                 u"The expected fields are:\n\n%s\n\nwhile "
                                 u"the headers found in the test file are:\n\n"
                                 u"%s\n\nUse --no-test-header flag if first li"
                                 u"ne should not be interpreted as headers." %
                                 (",".join(fields_names),
                                  ",".join(headers))).encode("utf-8"))

    prediction_file = output
    output_path = check_dir(output)
    output = open(output, 'w', 0)
    if resume:
        number_of_tests = file_number_of_lines(test_set)
        if test_set_header:
            number_of_tests -= 1
    # Remote predictions: predictions are computed in bigml.com and stored
    # in a file named after the model in the following syntax:
    #     model_[id of the model]__predictions.csv
    # For instance,
    #     model_50c0de043b563519830001c2_predictions.csv
    if remote:

        predictions_files = []
        prediction_args = {
            "tags": tags
        }
        for model in models:
            if not isinstance(model, basestring) and 'resource' in model:
                model = model['resource']
            predictions_file = get_predictions_file_name(model,
                                                         output_path)
            predictions_files.append(predictions_file)
            if resume:
                resume = checkpoint(are_predictions_created, predictions_file,
                                    number_of_tests)
            if not resume:
                message = "[%s] Creating remote predictions.\n" % get_date()
                log_message(message, log_file=session_file, console=verbosity)

                predictions_file = csv.writer(open(predictions_file, 'w', 0))
                for row in test_reader:
                    for index in exclude:
                        del row[index]
                    input_data = fields.pair(row, headers, objective_field)
                    prediction = api.create_prediction(model, input_data,
                                                       by_name=test_set_header,
                                                       wait_time=0,
                                                       args=prediction_args)
                    if log:
                        log.write("%s\n" % prediction['resource'])
                        log.flush()
                    prediction_row = prediction_to_row(prediction)
                    predictions_file.writerow(prediction_row)
        combine_votes(predictions_files,
                      Model(models[0]).to_prediction,
                      prediction_file, method)
    # Local predictions: Predictions are computed locally using models' rules
    # with MultiModel's predict method
    else:
        message = "[%s] Creating local predictions.\n" % get_date()
        log_message(message, log_file=session_file, console=verbosity)
        models_total = len(models)
        # For a small number of models, we build a MultiModel using all of
        # the given models and issue a combined prediction
        if models_total < max_models:
            local_model = MultiModel(models)
            for row in test_reader:
                for index in exclude:
                    del row[index]
                input_data = fields.pair(row, headers, objective_field)
                prediction = local_model.predict(input_data,
                                                 by_name=test_set_header,
                                                 method=method)
                write_prediction(prediction, output)
        # For large numbers of models, we split the list of models in chunks
        # and build a MultiModel for each chunk, issue and store predictions
        # for each model and combine all of them eventually.
        else:
            models_splits = [models[index:(index + max_models)] for index
                             in range(0, models_total, max_models)]
            input_data_list = []
            for row in test_reader:
                for index in exclude:
                    del row[index]
                input_data_list.append(fields.pair(row, headers,
                                                   objective_field))
            total_votes = []
            models_count = 0
            for models_split in models_splits:
                if resume:
                    for model in models_split:
                        pred_file = get_predictions_file_name(model,
                                                              output_path)
                        checkpoint(are_predictions_created,
                                   pred_file,
                                   number_of_tests)
                complete_models = []
                for index in range(len(models_split)):
                    complete_models.append(api.check_resource(
                        models_split[index], api.get_model))

                local_model = MultiModel(complete_models)
                local_model.batch_predict(input_data_list,
                                          output_path, reuse=True)
                votes = local_model.batch_votes(output_path)
                models_count += max_models
                if models_count > models_total:
                    models_count = models_total
                if verbosity:
                    draw_progress_bar(models_count, models_total)
                if total_votes:
                    for index in range(0, len(votes)):
                        predictions = total_votes[index].predictions
                        predictions.extend(votes[index].predictions)
                else:
                    total_votes = votes
            message = "[%s] Combining predictions.\n" % get_date()
            log_message(message, log_file=session_file, console=verbosity)
            for multivote in total_votes:
                write_prediction(multivote.combine(method), output)

    console_log("")
    output.close()


def compute_output(api, args, training_set, test_set=None, output=None,
                   objective_field=None,
                   description=None,
                   field_attributes=None,
                   types=None,
                   dataset_fields=None,
                   model_fields=None,
                   name=None, training_set_header=True,
                   test_set_header=True, model_ids=None,
                   votes_files=None, resume=False, fields_map=None):
    """ Creates one or more models using the `training_set` or uses the ids
    of previously created BigML models to make predictions for the `test_set`.

    """
    source = None
    dataset = None
    model = None
    models = None
    fields = None

    path = check_dir(output)
    session_file = "%s%s%s" % (path, os.sep, SESSIONS_LOG)
    csv_properties = {}
    # If logging is required, open the file for logging
    log = None
    if args.log_file:
        check_dir(args.log_file)
        # If --clear_logs the log files are cleared
        if args.clear_logs:
            try:
                open(log, 'w', 0).close()
            except IOError:
                pass
        log = open(args.log_file, 'a', 0)

    if (training_set or (args.evaluate and test_set)):
        if resume:
            resume, args.source = checkpoint(is_source_created,
                                             path, bigml.api)
            if not resume:
                message = "[%s] Source not found. Resuming.\n" % get_date()
                log_message(message, log_file=session_file,
                            console=args.verbosity)

    # If neither a previous source, dataset or model are provided.
    # we create a new one. Also if --evaluate and test data are provided
    # we create a new dataset to test with.
    data_set = None
    if (training_set and not args.source and not args.dataset and
            not args.model and not args.models):
        data_set = training_set
        data_set_header = training_set_header
    elif (args.evaluate and test_set and not args.source):
        data_set = test_set
        data_set_header = test_set_header

    if not data_set is None:

        source_args = {
            "name": name,
            "description": description,
            "category": args.category,
            "tags": args.tag,
            "source_parser": {"header": data_set_header}}
        message = "[%s] Creating source.\n" % get_date()
        log_message(message, log_file=session_file, console=args.verbosity)
        source = api.create_source(data_set, source_args,
                                   progress_bar=args.progress_bar)
        source = api.check_resource(source, api.get_source)
        message = "[%s] Source created: %s\n" % (get_date(),
                                                 get_url(source, api))
        log_message(message, log_file=session_file, console=args.verbosity)
        if log:
            log.write("%s\n" % source['resource'])
            log.flush()

        fields = Fields(source['object']['fields'],
                        source['object']['source_parser']['missing_tokens'],
                        source['object']['source_parser']['locale'])
        source_file = open(path + '/source', 'w', 0)
        source_file.write("%s\n" % source['resource'])
        source_file.write("%s\n" % source['object']['name'])
        source_file.flush()
        source_file.close()

    # If a source is provided, we retrieve it.
    elif args.source:
        message = "[%s] Retrieving source. %s\n" % (get_date(),
                  get_url(args.source, api))
        log_message(message, log_file=session_file, console=args.verbosity)
        source = api.get_source(args.source)

    # If we already have source, we check that is finished and extract the
    # fields, and update them if needed.
    if source:
        if source['object']['status']['code'] != bigml.api.FINISHED:
            message = "[%s] Retrieving source. %s\n" % (get_date(),
                                                        get_url(source, api))
            log_message(message, log_file=session_file, console=args.verbosity)
            source = api.check_resource(source, api.get_source)
        csv_properties = {'missing_tokens':
                          source['object']['source_parser']['missing_tokens'],
                          'data_locale':
                          source['object']['source_parser']['locale']}

        fields = Fields(source['object']['fields'], **csv_properties)
        update_fields = {}
        if field_attributes:
            for (column, value) in field_attributes.iteritems():
                update_fields.update({
                    fields.field_id(column): value})
            message = "[%s] Updating source. %s\n" % (get_date(),
                                                      get_url(source, api))
            log_message(message, log_file=session_file, console=args.verbosity)
            source = api.update_source(source, {"fields": update_fields})

        update_fields = {}
        if types:
            for (column, value) in types.iteritems():
                update_fields.update({
                    fields.field_id(column): {'optype': value}})
            message = "[%s] Updating source. %s\n" % (get_date(),
                                                      get_url(source, api))
            log_message(message, log_file=session_file, console=args.verbosity)
            source = api.update_source(source, {"fields": update_fields})

    if (training_set or args.source or (args.evaluate and test_set)):
        if resume:
            resume, args.dataset = checkpoint(is_dataset_created,
                                              path, bigml.api)
            if not resume:
                message = "[%s] Dataset not found. Resuming.\n" % get_date()
                log_message(message, log_file=session_file,
                            console=args.verbosity)
    # If we have a source but not dataset or model has been provided, we
    # create a new dataset if the no_dataset option isn't set up. Also
    # if evaluate is set and test_set has been provided.
    if ((source and not args.dataset and not args.model and not model_ids and
            not args.no_dataset) or
            (args.evaluate and args.test_set and not args.dataset)):
        dataset_args = {
            "name": name,
            "description": description,
            "category": args.category,
            "tags": args.tag
        }

        if args.json_filter:
            dataset_args.update(json_filter=args.json_filter)
        elif args.lisp_filter:
            dataset_args.update(lisp_filter=args.lisp_filter)

        input_fields = []
        if dataset_fields:
            for name in dataset_fields:
                input_fields.append(fields.field_id(name))
            dataset_args.update(input_fields=input_fields)
        message = "[%s] Creating dataset.\n" % get_date()
        log_message(message, log_file=session_file, console=args.verbosity)
        dataset = api.create_dataset(source, dataset_args)
        dataset = api.check_resource(dataset, api.get_dataset)
        message = "[%s] Dataset created: %s\n" % (get_date(),
                                                  get_url(dataset, api))
        log_message(message, log_file=session_file, console=args.verbosity)
        if log:
            log.write("%s\n" % dataset['resource'])
            log.flush()
        dataset_file = open(path + '/dataset', 'w', 0)
        dataset_file.write("%s\n" % dataset['resource'])
        dataset_file.flush()
        dataset_file.close()

    # If a dataset is provided, let's retrieve it.
    elif args.dataset:
        message = "[%s] Retrieving dataset. %s\n" % (get_date(),
                  get_url(args.dataset, api))
        log_message(message, log_file=session_file, console=args.verbosity)
        dataset = api.get_dataset(args.dataset)

    # If we already have a dataset, we check the status and get the fields if
    # we hadn't them yet.
    if dataset:
        if dataset['object']['status']['code'] != bigml.api.FINISHED:
            message = "[%s] Retrieving dataset. %s\n" % (get_date(),
                      get_url(dataset, api))
            log_message(message, log_file=session_file, console=args.verbosity)
            dataset = api.check_resource(dataset, api.get_dataset)
        if not csv_properties:
            csv_properties = {'data_locale':
                              dataset['object']['locale']}
        if args.public_dataset:
            public_dataset = {"private": False}
            if args.dataset_price:
                message = "[%s] Updating dataset. %s\n" % (get_date(),
                          get_url(dataset, api))
                log_message(message, log_file=session_file,
                            console=args.verbosity)
                public_dataset.update(price=args.dataset_price)
            message = "[%s] Updating dataset. %s\n" % (get_date(),
                      get_url(dataset, api))
            log_message(message, log_file=session_file,
                        console=args.verbosity)
            dataset = api.update_dataset(dataset, public_dataset)
        fields = Fields(dataset['object']['fields'], **csv_properties)

    # If we have a dataset but not a model, we create the model if the no_model
    # flag hasn't been set up.
    if (dataset and not args.model and not model_ids and not args.no_model):
        model_args = {
            "name": name,
            "description": description,
            "category": args.category,
            "tags": args.tag
        }
        if not objective_field is None:
            model_args.update({"objective_field":
                               fields.field_id(objective_field)})
        # If evaluate flag is on, we choose a deterministic sampling with 80%
        # of the data to create the model
        if args.evaluate:
            if args.sample_rate == 1:
                args.sample_rate = EVALUATE_SAMPLE_RATE
            seed = SEED
            model_args.update(seed=seed)

        input_fields = []
        if model_fields:
            for name in model_fields:
                input_fields.append(fields.field_id(name))
            model_args.update(input_fields=input_fields)

        if args.pruning and args.pruning != 'smart':
            model_args.update(stat_pruning=(args.pruning == 'statistical'))

        model_args.update(sample_rate=args.sample_rate,
                          replacement=args.replacement,
                          randomize=args.randomize)
        model_ids = []
        models = []
        if resume:
            resume, model_ids = checkpoint(are_models_created, path,
                                           args.number_of_models,
                                           bigml.api)
            if not resume:
                message = ("[%s] Found %s models out of %s. Resuming.\n" %
                           (get_date(), len(model_ids),
                            args.number_of_models))
                log_message(message, log_file=session_file,
                            console=args.verbosity)
            models = model_ids
            args.number_of_models -= len(model_ids)

        model_file = open(path + '/models', 'w', 0)
        for model_id in model_ids:
            model_file.write("%s\n" % model_id)
        last_model = None
        if args.number_of_models > 0:
            plural = "s" if args.number_of_models > 1 else ""
            message = "[%s] Creating model%s.\n" % (get_date(), plural)
            log_message(message, log_file=session_file, console=args.verbosity)
            for i in range(1, args.number_of_models + 1):
                if i > args.max_parallel_models:
                    api.check_resource(last_model, api.get_model)
                model = api.create_model(dataset, model_args)
                if log:
                    log.write("%s\n" % model['resource'])
                    log.flush()
                last_model = model
                model_ids.append(model['resource'])
                models.append(model)
                model_file.write("%s\n" % model['resource'])
                model_file.flush()
            if args.number_of_models < 2 and args.verbosity:
                if model['object']['status']['code'] != bigml.api.FINISHED:
                    model = api.check_resource(model, api.get_model)
                    models[0] = model
                message = "[%s] Model created: %s.\n" % (get_date(),
                          get_url(model, api))
                log_message(message, log_file=session_file,
                            console=args.verbosity)
        model_file.close()

    # If a model is provided, we retrieve it.
    elif args.model:
        message = "[%s] Retrieving model. %s\n" % (get_date(),
                  get_url(args.model, api))
        log_message(message, log_file=session_file, console=args.verbosity)
        model = api.get_model(args.model)

    elif args.models or args.model_tag:
        models = model_ids[:]

    if model_ids and test_set and not args.evaluate:
        plural, model_id = ("", "")
        if len(model_ids) > 1:
            plural = "s"
        else:
            model_id = model_ids[0]
        message = "[%s] Retrieving model%s. %s\n" % (get_date(),
                  plural, get_url(model_id, api))
        log_message(message, log_file=session_file, console=args.verbosity)
        if len(model_ids) < args.max_batch_models:
            models = []
            for model in model_ids:
                model = api.check_resource(model, api.get_model)
                models.append(model)
            model = models[0]
        else:
            model = api.check_resource(model_ids[0], api.get_model)
            models[0] = model

    # We check that the model is finished and get the fields if haven't got
    # them yet.
    if model and not args.evaluate and (test_set or args.black_box
                                        or args.white_box):
        if model['object']['status']['code'] != bigml.api.FINISHED:
            message = "[%s] Retrieving model. %s\n" % (get_date(),
                      get_url(model, api))
            log_message(message, log_file=session_file, console=args.verbosity)
            model = api.check_resource(model, api.get_model)
        if args.black_box:
            model = api.update_model(model, {"private": False})
        if args.white_box:
            public_model = {"private": False, "white_box": True}
            if args.model_price:
                message = "[%s] Updating model. %s\n" % (get_date(),
                          get_url(model, api))
                log_message(message, log_file=session_file,
                            console=args.verbosity)
                public_model.update(price=args.model_price)
            if args.cpp:
                message = "[%s] Updating model. %s\n" % (get_date(),
                          get_url(model, api))
                log_message(message, log_file=session_file,
                            console=args.verbosity)
                public_model.update(credits_per_prediction=args.cpp)
            model = api.update_model(model, public_model)
        if not csv_properties:
            csv_properties = {'data_locale':
                              model['object']['locale']}
        csv_properties.update(verbose=True)
        if args.user_locale:
            csv_properties.update(data_locale=args.user_locale)

        fields = Fields(model['object']['model']['fields'], **csv_properties)

    if model and not models:
        models = [model]

    if models and test_set and not args.evaluate:
        objective_field = models[0]['object']['objective_fields']
        if isinstance(objective_field, list):
            objective_field = objective_field[0]
        predict(test_set, test_set_header, models, fields, output,
                objective_field, args.remote, api, log,
                args.max_batch_models, args.method, resume, args.tag,
                args.verbosity)

    # When combine_votes flag is used, retrieve the predictions files saved
    # in the comma separated list of directories and combine them
    if votes_files:
        model_id = re.sub(r'.*(model_[a-f0-9]{24})__predictions\.csv$',
                          r'\1', votes_files[0]).replace("_", "/")
        model = api.check_resource(model_id, api.get_model)
        local_model = Model(model)
        message = "[%s] Combining votes.\n" % get_date()
        log_message(message, log_file=session_file,
                    console=args.verbosity)
        combine_votes(votes_files, local_model.to_prediction,
                      output, args.method)

    # If evaluate flag is on, create remote evaluation and save results in
    # json and human-readable format.
    if args.evaluate:
        if resume:
            resume, evaluation = checkpoint(is_evaluation_created,
                                            path, bigml.api)
            if not resume:
                message = "[%s] Evaluation not found. Resuming.\n" % get_date()
                log_message(message, log_file=session_file,
                            console=args.verbosity)
        if not resume:
            evaluation_file = open(path + '/evaluation', 'w', 0)
            evaluation_args = {
                "name": name,
                "description": description,
                "tags": args.tag
            }
            if not fields_map is None:
                update_map = {}
                for (dataset_column, model_column) in fields_map.iteritems():
                    update_map.update({
                        fields.field_id(dataset_column):
                        fields.field_id(model_column)})
                evaluation_args.update({"fields_map": update_map})
            if not ((args.dataset or args.test_set)
                    and (args.model or args.models or args.model_tag)):
                seed = SEED
                evaluation_args.update(out_of_bag=True, seed=seed,
                                       sample_rate=args.sample_rate)
            message = "[%s] Creating evaluation.\n" % get_date()
            log_message(message, log_file=session_file, console=args.verbosity)
            evaluation = api.create_evaluation(model, dataset, evaluation_args)
            if log:
                log.write("%s\n" % evaluation['resource'])
                log.flush()
            evaluation_file.write("%s\n" % evaluation['resource'])
            evaluation_file.flush()
            evaluation_file.close()
        message = "[%s] Retrieving evaluation. %s\n" % (get_date(),
                  get_url(evaluation, api))
        log_message(message, log_file=session_file, console=args.verbosity)
        evaluation = api.check_resource(evaluation, api.get_evaluation)
        evaluation_json = open(output + '.json', 'w', 0)
        evaluation_json.write(json.dumps(evaluation['object']['result']))
        evaluation_json.flush()
        evaluation_json.close()
        evaluation_txt = open(output + '.txt', 'w', 0)
        api.pprint(evaluation['object']['result'],
                   evaluation_txt)
        evaluation_txt.flush()
        evaluation_txt.close()

    if args.log_file and log:
        log.close()
    # Workaround to restore windows console cp850 encoding to print the tree
    if sys.platform == "win32" and sys.stdout.isatty():
        import locale
        data_locale = locale.getlocale()
        if not data_locale[0] is None:
            locale.setlocale(locale.LC_ALL, (data_locale[0], "850"))
        message = (u"\nGenerated files:\n\n" +
                   unicode(print_tree(path, " "), "utf-8") + u"\n")
    else:
        message = "\nGenerated files:\n\n" + print_tree(path, " ") + "\n"
    log_message(message, log_file=session_file, console=args.verbosity)


def main(args=sys.argv[1:]):
    """Main process

    """
    for i in range(0, len(args)):
        if args[i].startswith("--"):
            args[i] = args[i].replace("_", "-")
    # If --clear-logs the log files are cleared
    if "--clear-logs" in args:
        for log_file in LOG_FILES:
            try:
                open(log_file, 'w', 0).close()
            except IOError:
                pass
    for i in range(0, len(args)):
        if ' ' in args[i]:
            args[i] = '"%s"' % args[i]
    message = "bigmler %s\n" % " ".join(args)

    # Resume calls are not logged
    if not "--resume" in args:
        command_log = open(COMMAND_LOG, "a", 0)
        command_log.write(message)
        command_log.close()
        resume = False

    parser = create_parser(defaults=get_user_defaults(), constants={'NOW': NOW,
                           'MAX_MODELS': MAX_MODELS, 'PLURALITY': PLURALITY})

    # Parses command line arguments.
    command_args = parser.parse_args(args)

    default_output = ('evaluation' if command_args.evaluate
                      else 'predictions.csv')
    if command_args.resume:
        command = get_log_reversed(COMMAND_LOG,
                                   command_args.stack_level)
        args = shlex.split(command)[1:]
        output_dir = get_log_reversed(DIRS_LOG,
                                      command_args.stack_level)
        defaults_file = "%s%s%s" % (output_dir, os.sep, DEFAULTS_FILE)
        parser = create_parser(defaults=get_user_defaults(defaults_file),
                               constants={'NOW': NOW, 'MAX_MODELS': MAX_MODELS,
                                          'PLURALITY': PLURALITY})
        command_args = parser.parse_args(args)
        if command_args.predictions is None:
            command_args.predictions = ("%s%s%s" %
                                        (output_dir, os.sep,
                                         default_output))
        # Logs the issued command and the resumed command
        session_file = "%s%s%s" % (output_dir, os.sep, SESSIONS_LOG)
        log_message(message, log_file=session_file)
        message = "\nResuming command:\n%s\n\n" % command
        log_message(message, log_file=session_file, console=True)
        try:
            defaults_handler = open(defaults_file, 'r')
            contents = defaults_handler.read()
            message = "\nUsing the following defaults:\n%s\n\n" % contents
            log_message(message, log_file=session_file, console=True)
            defaults_handler.close()
        except IOError:
            pass

        resume = True
    else:
        if command_args.predictions is None:
            command_args.predictions = ("%s%s%s" %
                                        (NOW, os.sep,
                                         default_output))
        if len(os.path.dirname(command_args.predictions).strip()) == 0:
            command_args.predictions = ("%s%s%s" %
                                        (NOW, os.sep,
                                         command_args.predictions))
        directory = check_dir(command_args.predictions)
        session_file = "%s%s%s" % (directory, os.sep, SESSIONS_LOG)
        log_message(message + "\n", log_file=session_file)
        try:
            defaults_file = open(DEFAULTS_FILE, 'r')
            contents = defaults_file.read()
            defaults_file.close()
            defaults_copy = open("%s%s%s" % (directory, os.sep, DEFAULTS_FILE),
                                 'w', 0)
            defaults_copy.write(contents)
            defaults_copy.close()
        except IOError:
            pass
        directory_log = open(DIRS_LOG, "a", 0)
        directory_log.write("%s\n" % os.path.abspath(directory))
        directory_log.close()

    api_command_args = {
        'username': command_args.username,
        'api_key': command_args.api_key,
        'dev_mode': command_args.dev_mode,
        'debug': command_args.debug}

    api = bigml.api.BigML(**api_command_args)

    if (command_args.evaluate
        and not (command_args.training_set or command_args.source
                 or command_args.dataset)
        and not (command_args.test_set and (command_args.model or
                 command_args.models or command_args.model_tag))):
        parser.error("Evaluation wrong syntax.\n"
                     "\nTry for instance:\n\nbigmler --train data/iris.csv"
                     " --evaluate\nbigmler --model "
                     "model/5081d067035d076151000011 --dataset "
                     "dataset/5081d067035d076151003423 --evaluate")

    output_args = {
        "api": api,
        "training_set": command_args.training_set,
        "test_set": command_args.test_set,
        "output": command_args.predictions,
        "objective_field": command_args.objective_field,
        "name": command_args.name,
        "training_set_header": command_args.train_header,
        "test_set_header": command_args.test_header,
        "args": command_args,
        "resume": resume,
    }

    # Reads description if provided.
    if command_args.description:
        description_arg = read_description(command_args.description)
        output_args.update(description=description_arg)
    else:
        output_args.update(description="Created using BigMLer")

    # Parses fields if provided.
    if command_args.field_attributes:
        field_attributes_arg = (
            read_field_attributes(command_args.field_attributes))
        output_args.update(field_attributes=field_attributes_arg)

    # Parses types if provided.
    if command_args.types:
        types_arg = read_types(command_args.types)
        output_args.update(types=types_arg)

    # Parses dataset fields if provided.
    if command_args.dataset_fields:
        dataset_fields_arg = map(lambda x: x.strip(),
                                 command_args.dataset_fields.split(','))
        output_args.update(dataset_fields=dataset_fields_arg)

    # Parses model input fields if provided.
    if command_args.model_fields:
        model_fields_arg = map(lambda x: x.strip(),
                               command_args.model_fields.split(','))
        output_args.update(model_fields=model_fields_arg)

    model_ids = []
    # Parses model/ids if provided.
    if command_args.models:
        model_ids = read_models(command_args.models)
        output_args.update(model_ids=model_ids)

    dataset_id = None
    # Parses dataset/id if provided.
    if command_args.datasets:
        dataset_id = read_dataset(command_args.datasets)
        command_args.dataset = dataset_id

    # Retrieve model/ids if provided.
    if command_args.model_tag:
        model_ids = (model_ids +
                     list_model_ids(api,
                                    "tags__in=%s" % command_args.model_tag))
        output_args.update(model_ids=model_ids)

    # Reads a json filter if provided.
    if command_args.json_filter:
        json_filter = read_json_filter(command_args.json_filter)
        command_args.json_filter = json_filter

    # Reads a lisp filter if provided.
    if command_args.lisp_filter:
        lisp_filter = read_lisp_filter(command_args.lisp_filter)
        command_args.lisp_filter = lisp_filter

    # Adds default tags unless that it is requested not to do so.
    if command_args.no_tag:
        command_args.tag.append('BigMLer')
        command_args.tag.append('BigMLer_%s' % NOW)

    # Checks combined votes method
    if (command_args.method and
            not command_args.method in COMBINATION_WEIGHTS.keys()):
        command_args.method = 0
    else:
        combiner_methods = dict([[value, key]
                                for key, value in COMBINER_MAP.items()])
        command_args.method = combiner_methods.get(command_args.method, 0)

    # Reads votes files in the provided directories.
    if command_args.votes_dirs:
        dirs = map(lambda x: x.strip(), command_args.votes_dirs.split(','))
        votes_path = os.path.dirname(command_args.predictions)
        votes_files = read_votes_files(dirs, votes_path)
        output_args.update(votes_files=votes_files)

    # Parses fields map if provided.
    if command_args.fields_map:
        fields_map_arg = read_fields_map(command_args.fields_map)
        output_args.update(fields_map=fields_map_arg)

    # Parses resources ids if provided.
    if command_args.delete:
        if command_args.predictions is None:
            path = NOW
        else:
            path = check_dir(command_args.predictions)
        session_file = "%s%s%s" % (path, os.sep, SESSIONS_LOG)
        message = "[%s] Retrieving objects to delete.\n" % get_date()
        log_message(message, log_file=session_file,
                    console=command_args.verbosity)
        delete_list = []
        if command_args.delete_list:
            delete_list = map(lambda x: x.strip(),
                              command_args.delete_list.split(','))
        if command_args.delete_file:
            if not os.path.exists(command_args.delete_file):
                raise Exception("File %s not found" % command_args.delete_file)
            delete_list.extend([line for line
                                in open(command_args.delete_file, "r")])
        if command_args.all_tag:
            query_string = "tags__in=%s" % command_args.all_tag
            delete_list.extend(list_source_ids(api, query_string))
            delete_list.extend(list_dataset_ids(api, query_string))
            delete_list.extend(list_model_ids(api, query_string))
            delete_list.extend(list_prediction_ids(api, query_string))
            delete_list.extend(list_evaluation_ids(api, query_string))
        # Retrieve sources/ids if provided
        if command_args.source_tag:
            query_string = "tags__in=%s" % command_args.source_tag
            delete_list.extend(list_source_ids(api, query_string))
        # Retrieve datasets/ids if provided
        if command_args.dataset_tag:
            query_string = "tags__in=%s" % command_args.dataset_tag
            delete_list.extend(list_dataset_ids(api, query_string))
        # Retrieve model/ids if provided
        if command_args.model_tag:
            query_string = "tags__in=%s" % command_args.model_tag
            delete_list.extend(list_model_ids(api, query_string))
        # Retrieve prediction/ids if provided
        if command_args.prediction_tag:
            query_string = "tags__in=%s" % command_args.prediction_tag
            delete_list.extend(list_prediction_ids(api, query_string))
        # Retrieve evaluation/ids if provided
        if command_args.evaluation_tag:
            query_string = "tags__in=%s" % command_args.evaluation_tag
            delete_list.extend(list_evaluation_ids(api, query_string))
        message = "[%s] Deleting objects.\n" % get_date()
        log_message(message, log_file=session_file,
                    console=command_args.verbosity)
        message = "\n".join(delete_list)
        log_message(message, log_file=session_file)
        delete(api, delete_list)
        if sys.platform == "win32" and sys.stdout.isatty():
            message = (u"\nGenerated files:\n\n" +
                       unicode(print_tree(path, " "), "utf-8") + u"\n")
        else:
            message = "\nGenerated files:\n\n" + print_tree(path, " ") + "\n"
        log_message(message, log_file=session_file,
                    console=command_args.verbosity)
        if command_args.verbosity:
            console_log("")
    elif (command_args.training_set or command_args.test_set
          or command_args.source or command_args.dataset
          or command_args.datasets or command_args.votes_dirs):
        compute_output(**output_args)
    log_message("_" * 80 + "\n", log_file=session_file)

if __name__ == '__main__':
    main(sys.argv[1:])
