"""
Library for converting raw data into feature vectors.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

import collections
import functools
import numpy as np
import pickle
import random
import re
import os, sys

if sys.version_info > (3, 0):
    from six.moves import xrange

from bashlint import bash, nast, data_tools
from nlp_tools import constants, ops, slot_filling, tokenizer

# Special token symbols
_PAD = "__SP__PAD"
_EOS = "__SP__EOS"
_UNK = "__SP__UNK"
_ARG_UNK = "__SP__ARGUMENT_UNK"
_UTL_UNK = "__SP__UTILITY_UNK"
_FLAG_UNK = "__SP__FLAG_UNK"
_ARG_START = "__SP__ARG_START"
_ARG_END = "__SP__ARG_END"

_GO = "__SP__GO"                   # seq2seq start symbol
_ROOT = "__SP__ROOT"               # seq2tree start symbol

PAD_ID = 0
EOS_ID = 1
UNK_ID = 2
ARG_UNK_ID = 3
UTL_UNK_ID = 4
FLAG_UNK_ID = 5
H_NO_EXPAND_ID = 6
V_NO_EXPAND_ID = 7
GO_ID = 8
ROOT_ID = 9
ARG_START_ID = 10                  # start argument sketch
ARG_END_ID = 11                    # end argument sketch
NUM_ID = 12                        # 1, 2, 3, 4, ...
NUM_ALPHA_ID = 13                  # 10k, 20k, 50k, 100k, ...
NON_ENGLISH_ID = 14                # /local/bin, hello.txt, ...

TOKEN_INIT_VOCAB = [
    _PAD,
    _EOS,
    _UNK,
    _ARG_UNK,
    _UTL_UNK,
    _FLAG_UNK,
    nast._H_NO_EXPAND,
    nast._V_NO_EXPAND,
    _GO,
    _ROOT,
    _ARG_START,
    _ARG_END,
    constants._NUMBER,
    constants._NUMBER_ALPHA,
    constants._REGEX
]

# Special char symbols
_CPAD = "__SP__CPAD"
_CEOS = "__SP__CEOS"
_CUNK = "__SP__CUNK"
_CATOM = "__SP__CATOM"
_CGO = "__SP__CGO"

CPAD_ID = 0
CEOS_ID = 1
CUNK_ID = 2
CATOM_ID = 3
CLONG_ID = 4
CGO_ID = 5

CHAR_INIT_VOCAB = [
    _CPAD,
    _CEOS,
    _CUNK,
    _CATOM,
    constants._LONG_TOKEN_IND,
    _CGO
]

data_splits = ['train', 'dev', 'test']
TOKEN_SEPARATOR = '||||'


class DataSet(object):
    def __init__(self):
        self.data_points = []
        self.max_sc_length = -1
        self.max_tg_length = -1
        self.buckets = None


class DataPoint(object):
    def __init__(self):
        self.sc_txt = None
        self.tg_txt = None
        self.sc_ids = None
        self.tg_ids = None
        self.csc_ids = None         # CopyNet training source ids
        self.ctg_ids = None         # CopyNet training target ids
        self.alignments = None
        self.sc_fillers = None      # TODO: this field is no longer used


class Vocab(object):
    def __init__(self):
        self.sc_vocab = None
        self.tg_vocab = None
        self.rev_sc_vocab = None
        self.rev_tg_vocab = None
        self.max_sc_token_size = -1
        self.max_tg_token_size = -1


# --- Data IO --- #

def load_data(FLAGS, use_buckets=True, load_mappings=False):
    print("Loading data from %s" % FLAGS.data_dir)

    source, target = ('nl', 'cm') if not FLAGS.explain else ('cm', 'nl')

    train_set = read_data(FLAGS, 'train', source, target,
        use_buckets=use_buckets, add_start_token=True, add_end_token=True)
    dev_set = read_data(FLAGS, 'dev', source, target,
        use_buckets=use_buckets, buckets=train_set.buckets,
        add_start_token=True, add_end_token=True)
    test_set = read_data(FLAGS, 'test', source, target,
        use_buckets=use_buckets, buckets=train_set.buckets,
        add_start_token=True, add_end_token=True)

    return train_set, dev_set, test_set


def read_data(FLAGS, split, source, target, use_buckets=True, buckets=None,
              add_start_token=False, add_end_token=False):
    vocab = load_vocabulary(FLAGS)
    svf, tvf = load_vocabulary_frequency(FLAGS)

    def get_data_file_path(data_dir, split, lang, channel):
        return os.path.join(data_dir, '{}.{}.{}'.format(split, lang, channel))

    def get_source_ids(s):
        source_ids = []
        for x in s.split():
            ind = int(x)
            token = vocab.rev_sc_vocab[ind] if ind in vocab.rev_sc_vocab else ''
            if '<FLAG_SUFFIX>' in token or svf[ind] >= FLAGS.min_vocab_frequency:
                source_ids.append(ind)
            else:
                source_ids.append(UNK_ID)
        return source_ids

    def get_target_ids(s):
        target_ids = []
        for x in s.split():
            ind = int(x)
            token = vocab.rev_tg_vocab[ind] if ind in vocab.rev_tg_vocab else ''
            if '<FLAG_SUFFIX>' in token or tvf[ind] >= FLAGS.min_vocab_frequency:
                target_ids.append(ind)
            else:
                target_ids.append(UNK_ID)
        if add_start_token:
            target_ids.insert(0, ROOT_ID)
        if add_end_token:
            target_ids.append(EOS_ID)
        return target_ids

    data_dir = FLAGS.data_dir
    sc_path = get_data_file_path(data_dir, split, source, 'filtered')
    tg_path = get_data_file_path(data_dir, split, target, 'filtered')
    if FLAGS.char:
        channel = 'char'
    elif FLAGS.partial_token:
        channel = 'partial.token'
    else:
        channel = 'token'
    sc_id_path = get_data_file_path(data_dir, split, source, 'ids.'+channel)
    tg_id_path = get_data_file_path(data_dir, split, target, 'ids.'+channel)
    print("source file: {}".format(sc_path))
    print("target file: {}".format(tg_path))
    print("source sequence indices file: {}".format(sc_id_path))
    print("target sequence indices file: {}".format(tg_id_path))

    dataset = []
    num_data = 0
    max_sc_length = 0
    max_tg_length = 0
    with open(sc_path) as sc_file:
        with open(tg_path) as tg_file:
            with open(sc_id_path) as sc_id_file:
                with open(tg_id_path) as tg_id_file:
                    for sc_txt in sc_file.readlines():
                        data_point = DataPoint()
                        data_point.sc_txt = sc_txt
                        data_point.tg_txt = tg_file.readline().strip()
                        data_point.sc_ids = \
                            get_source_ids(sc_id_file.readline().strip())
                        if len(data_point.sc_ids) > max_sc_length:
                            max_sc_length = len(data_point.sc_ids)
                        data_point.tg_ids = \
                            get_target_ids(tg_id_file.readline().strip())
                        if len(data_point.tg_ids) > max_tg_length:
                            max_tg_length = len(data_point.tg_ids)
                        dataset.append(data_point)
                        num_data += 1
    print('{} data points read.'.format(num_data))
    print('max_source_length = {}'.format(max_sc_length))
    print('max_target_length = {}'.format(max_tg_length))

    if FLAGS.use_copy and FLAGS.copy_fun == 'copynet':
        sc_token_path = get_data_file_path(data_dir, split, source, channel)
        tg_token_path = get_data_file_path(data_dir, split, target, channel)
        with open(sc_token_path) as sc_token_file:
            with open(tg_token_path) as tg_token_file:
                for i, data_point in enumerate(dataset):
                    sc_tokens = sc_token_file.readline().strip().split(TOKEN_SEPARATOR)
                    tg_tokens = tg_token_file.readline().strip().split(TOKEN_SEPARATOR)
                    data_point.csc_ids, data_point.ctg_ids = \
                        compute_copy_indices(sc_tokens, tg_tokens, vocab.tg_vocab, channel)
                    # print(data_point.csc_ids)
                    # print(data_point.ctg_ids)
                    # print()
    data_size = len(dataset)

    def print_bucket_size(bs):
        print('bucket size = ({}, {})'.format(bs[0], bs[1]))

    if use_buckets:
        print('Group data points into buckets...')
        if split == 'train':
            # Compute bucket sizes, excluding outliers
            length_cutoff = 0.05 if FLAGS.char else 0.02
            # A. Determine maximum source length
            sorted_dataset = sorted(dataset, key=lambda x:len(x.sc_ids), reverse=True)
            max_sc_length = len(sorted_dataset[int(len(sorted_dataset) * length_cutoff)].sc_ids)
            # B. Determine maximum target length
            sorted_dataset = sorted(dataset, key=lambda x:len(x.tg_ids), reverse=True)
            max_tg_length = len(sorted_dataset[int(len(sorted_dataset) * length_cutoff)].tg_ids)
            print('max_source_length after filtering = {}'.format(max_sc_length))
            print('max_target_length after filtering = {}'.format(max_tg_length))
            num_buckets = 3
            min_bucket_sc, min_bucket_tg = 30, 30
            sc_inc = int((max_sc_length - min_bucket_sc) / (num_buckets-1)) + 1 \
                if max_sc_length > min_bucket_sc else 0
            tg_inc = int((max_tg_length - min_bucket_tg) / (num_buckets-1)) + 1 \
                if max_tg_length > min_bucket_tg else 0
            buckets = []
            for b in range(num_buckets):
                buckets.append((min_bucket_sc + b * sc_inc,
                                min_bucket_tg + b * tg_inc))
            buckets = sorted(list(set(buckets)), key=lambda x:100*x[0]+x[1])
        else:
            num_buckets = len(buckets)
            assert(num_buckets >= 1)

        dataset2 = [[] for b in xrange(num_buckets)]
        for i in range(len(dataset)):
            data_point = dataset[i]
            # compute bucket id
            bucket_ids = [b for b in xrange(len(buckets))
                          if buckets[b][0] > len(data_point.sc_ids) and
                          buckets[b][1] > len(data_point.tg_ids)]
            bucket_id = min(bucket_ids) if bucket_ids else (len(buckets)-1)
            dataset2[bucket_id].append(data_point)
        dataset = dataset2
        assert(len(functools.reduce(lambda x, y: x + y, dataset)) == data_size)
      
    D = DataSet()
    D.data_points = dataset
    if split == 'train':
        D.max_sc_length = max_sc_length
        D.max_tg_length = max_tg_length
        if use_buckets:
            D.buckets = buckets

    return D


def load_vocabulary(FLAGS):
    data_dir = FLAGS.data_dir
    source, target = ('nl', 'cm') if not FLAGS.explain else ('cm', 'nl')
    if FLAGS.char:
        vocab_ext = 'vocab.char'
    elif FLAGS.partial_token:
        vocab_ext = 'vocab.partial.token'
    else:
        vocab_ext = 'vocab.token'

    source_vocab_path = os.path.join(data_dir, '{}.{}'.format(source, vocab_ext))
    target_vocab_path = os.path.join(data_dir, '{}.{}'.format(target, vocab_ext))

    vocab = Vocab()
    min_vocab_frequency = 1 if FLAGS.char else FLAGS.min_vocab_frequency
    vocab.sc_vocab, vocab.rev_sc_vocab = initialize_vocabulary(
        source_vocab_path, min_vocab_frequency)
    vocab.tg_vocab, vocab.rev_tg_vocab = initialize_vocabulary(
        target_vocab_path, min_vocab_frequency)

    max_sc_token_size = 0
    for v in vocab.sc_vocab:
        if len(v) > max_sc_token_size:
            max_sc_token_size = len(v)
    max_tg_token_size = 0
    for v in vocab.tg_vocab:
        if len(v) > max_tg_token_size:
            max_tg_token_size = len(v)
    vocab.max_sc_token_size = max_sc_token_size
    vocab.max_tg_token_size = max_tg_token_size

    print('source vocabulary size = {}'.format(len(vocab.sc_vocab)))
    print('target vocabulary size = {}'.format(len(vocab.tg_vocab)))
    print('max source token size = {}'.format(vocab.max_sc_token_size))
    print('max target token size = {}'.format(vocab.max_tg_token_size))

    return vocab


def initialize_vocabulary(vocab_path, min_frequency=1):
    """Initialize vocabulary from file.

    We assume the vocabulary is stored one-item-per-line, so a file:
      dog
      cat
    will result in a vocabulary {"dog": 0, "cat": 1}, and this function will
    also return the reversed-vocabulary ["dog", "cat"].

    Args:
      vocab_path: path to the file containing the vocabulary.

    Returns:
      a pair: the vocabulary (a dictionary mapping string to integers), and
      the reversed vocabulary (a list, which reverses the vocabulary mapping).

    Raises:
      ValueError: if the provided vocab_path does not exist.
    """
    if tf.gfile.Exists(vocab_path):
        V= []
        with tf.gfile.GFile(vocab_path, mode="r") as f:
            while(True):
                line = f.readline()
                if line:
                    if line.startswith('\t'):
                        v = line[0]
                        freq = line.strip()   
                    else:
                        v, freq = line[:-1].rsplit('\t', 1)
                    if int(freq) >= min_frequency:
                        V.append(v)
                else:
                    break
        vocab = dict([(x, y) for (y, x) in enumerate(V)])
        rev_vocab = dict([(y, x) for (y, x) in enumerate(V)])
        assert(len(vocab) == len(rev_vocab))
        return vocab, rev_vocab
    else:
        raise ValueError("Vocabulary file %s not found.", vocab_path)


def load_vocabulary_frequency(FLAGS):
    data_dir = FLAGS.data_dir
    source, target = ('nl', 'cm') if not FLAGS.explain else ('cm', 'nl')
    if FLAGS.char:
        vocab_ext = 'vocab.char'
    elif FLAGS.partial_token:
        vocab_ext = 'vocab.partial.token'
    else:
        vocab_ext = 'vocab.token'

    source_vocab_path = os.path.join(data_dir, '{}.{}'.format(source, vocab_ext))
    target_vocab_path = os.path.join(data_dir, '{}.{}'.format(target, vocab_ext))

    sc_vocab_freq = initialize_vocabulary_frequency(source_vocab_path)
    tg_vocab_freq = initialize_vocabulary_frequency(target_vocab_path)

    return sc_vocab_freq, tg_vocab_freq


def initialize_vocabulary_frequency(vocab_path):
    vocab_freq = {}
    with open(vocab_path) as f:
        counter = 0
        for line in f:
            if line.startswith('\t'):
                v = line[0]
                freq = line.strip()
            else:
                v, freq = line.rsplit('\t', 1)
            vocab_freq[counter] = int(freq)
            counter += 1
    return vocab_freq


# --- Data Preparation --- #

def prepare_data(FLAGS):
    """
    Read a specified dataset, tokenize data, create vocabularies and save
    feature files.

    Save to disk:
        (1) nl vocabulary
        (2) cm vocabulary
        (3) nl token ids
        (4) cm token ids
    """
    data_dir = FLAGS.data_dir
    channel = FLAGS.channel if FLAGS.channel else ''
    prepare_dataset_split(data_dir, 'train', channel=channel)
    prepare_dataset_split(data_dir, 'dev', channel=channel)
    prepare_dataset_split(data_dir, 'test', channel=channel)


def prepare_dataset_split(data_dir, split, channel=''):
    """
    Process a specific dataset split.
    """
    def read_parallel_data(nl_path, cm_path):
        with open(nl_path) as f:
            nl_list = [nl.strip() for nl in f.readlines()]
        with open(cm_path) as f:
            cm_list = [cm.strip() for cm in f.readlines()]
        return nl_list, cm_list

    print("Split - {}".format(split))
    nl_path = os.path.join(data_dir, split + '.nl.filtered')
    cm_path = os.path.join(data_dir, split + '.cm.filtered')
    nl_list, cm_list = read_parallel_data(nl_path, cm_path)

    # character based processing
    if not channel or channel == 'char':
        prepare_channel(data_dir, nl_list, cm_list, split, channel='char',
                        parallel_data_to_tokens=parallel_data_to_characters,
                        nl_string_to_ids=tokens_to_ids,
                        cm_string_to_ids=tokens_to_ids)
    # partial-token based processing
    if not channel or channel == 'partial.token':
        prepare_channel(data_dir, nl_list, cm_list, split, channel='partial.token',
                        parallel_data_to_tokens=parallel_data_to_partial_tokens,
                        nl_string_to_ids=tokens_to_ids,
                        cm_string_to_ids=tokens_to_ids)
    # token based processing
    if not channel or channel == 'token':
        prepare_channel(data_dir, nl_list, cm_list, split, channel='token',
                        parallel_data_to_tokens=parallel_data_to_tokens,
                        nl_string_to_ids=tokens_to_ids,
                        cm_string_to_ids=tokens_to_ids)


def prepare_channel(data_dir, nl_list, cm_list, split, channel,
                    parallel_data_to_tokens, nl_string_to_ids, cm_string_to_ids):
    print("    channel - {}".format(channel))
    # Tokenize data
    nl_tokens, cm_tokens = parallel_data_to_tokens(nl_list, cm_list)
    nl_token_path = os.path.join(data_dir, '{}.nl.{}'.format(split, channel))
    cm_token_path = os.path.join(data_dir, '{}.cm.{}'.format(split, channel))
    with open(nl_token_path, 'w') as o_f:
        for data_point in nl_tokens:
            o_f.write('{}\n'.format(TOKEN_SEPARATOR.join(data_point)))
    with open(cm_token_path, 'w') as o_f:
        for data_point in cm_tokens:
            o_f.write('{}\n'.format(TOKEN_SEPARATOR.join(data_point)))
    # Create or load vocabulary
    nl_vocab_path = os.path.join(data_dir, 'nl.vocab.{}'.format(channel))
    cm_vocab_path = os.path.join(data_dir, 'cm.vocab.{}'.format(channel))
    if split == 'train':
        nl_vocab = create_vocabulary(nl_vocab_path, nl_tokens)
        cm_vocab = create_vocabulary(cm_vocab_path, cm_tokens)
    else:
        nl_vocab, _ = initialize_vocabulary(nl_vocab_path)
        cm_vocab, _ = initialize_vocabulary(cm_vocab_path)
    with open(os.path.join(data_dir, '{}.nl.ids.{}'.format(split, channel)), 'w') as o_f:
        for data_point in nl_tokens:
            nl_ids = nl_string_to_ids(data_point, nl_vocab)
            o_f.write('{}\n'.format(' '.join([str(x) for x in nl_ids])))
    with open(os.path.join(data_dir, '{}.cm.ids.{}'.format(split, channel)), 'w') as o_f:
        for data_point in cm_tokens:
            cm_ids = cm_string_to_ids(data_point, cm_vocab)
            o_f.write('{}\n'.format(' '.join([str(x) for x in cm_ids])))
    # For copying
    alignments = compute_alignments(data_dir, nl_tokens, cm_tokens, split, channel)
    with open(os.path.join(data_dir, '{}.{}.align'.format(split, channel)), 'wb') as f:
        pickle.dump(alignments, f)


def parallel_data_to_characters(nl_list, cm_list):
    nl_data = [nl_to_characters(nl) for nl in nl_list]
    cm_data = [cm_to_characters(cm) for cm in cm_list]
    return nl_data, cm_data


def parallel_data_to_partial_tokens(nl_list, cm_list):
    nl_data = [nl_to_partial_tokens(nl, tokenizer.basic_tokenizer) for nl in nl_list]
    cm_data = [cm_to_partial_tokens(cm, data_tools.bash_tokenizer) for cm in cm_list]
    return nl_data, cm_data


def parallel_data_to_tokens(nl_list, cm_list):
    nl_data = [nl_to_tokens(nl, tokenizer.basic_tokenizer) for nl in nl_list]
    cm_data = [cm_to_tokens(cm, data_tools.bash_tokenizer) for cm in cm_list]
    return nl_data, cm_data


def nl_to_characters(nl):
    nl_data_point = []
    nl_tokens = nl_to_tokens(nl, tokenizer.basic_tokenizer, lemmatization=False)
    for c in ' '.join(nl_tokens):
        if c == ' ':
            nl_data_point.append(constants._SPACE)
        else:
            nl_data_point.append(c)
    return nl_data_point


def cm_to_characters(cm):
    cm_data_point = []
    cm_tokens = cm_to_tokens(
        cm, data_tools.bash_tokenizer, with_prefix=False, with_suffix=False)
    for c in ' '.join(cm_tokens):
        if c == ' ':
            cm_data_point.append(constants._SPACE)
        else:
            cm_data_point.append(c)
    return cm_data_point


def nl_to_partial_tokens(s, tokenizer, lemmatization=True):
    return string_to_partial_tokens(
        nl_to_tokens(s, tokenizer, lemmatization=lemmatization), 
                     use_arg_start_end=False)


def cm_to_partial_tokens(s, tokenizer):
    return string_to_partial_tokens(cm_to_tokens(s, tokenizer))


def string_to_partial_tokens(s, use_arg_start_end=True):
    """
    Split a sequence of tokens into a sequence of partial tokens.

    A partial token may consist of
        1. continuous span of alphabetical letters
        2. continuous span of digits
        3. a non-alpha-numerical character
        4. _ARG_START which indicates the beginning of an argument token
        5. _ARG_END which indicates the end of an argument token
    """
    partial_tokens = []

    for token in s:
        if not token:
            continue
        if token.isalpha() or token.isnumeric() or '<FLAG_SUFFIX>' in token \
                or token in bash.binary_logic_operators \
                or token in bash.left_associate_unary_logic_operators \
                or token in bash.right_associate_unary_logic_operators:
            partial_tokens.append(token)
        else:
            arg_partial_tokens = []
            pt = ''
            reading_alpha = False
            reading_numeric = False
            for c in token:
                if reading_alpha:
                    if c.isalpha():
                        pt += c
                    else:
                        arg_partial_tokens.append(pt)
                        reading_alpha = False
                        pt = c
                        if c.isnumeric():
                            reading_numeric = True
                elif reading_numeric:
                    if c.isnumeric():
                        pt += c
                    else:
                        arg_partial_tokens.append(pt)
                        reading_numeric = False
                        pt = c
                        if c.isalpha():
                            reading_alpha = True
                else:
                    if pt:
                        arg_partial_tokens.append(pt)
                    pt = c
                    if c.isalpha():
                        reading_alpha = True
                    elif c.isnumeric():
                        reading_numeric = True
            if pt:
                arg_partial_tokens.append(pt)
            if len(arg_partial_tokens) > 1:
                if use_arg_start_end:
                    partial_tokens.append(_ARG_START)
                partial_tokens.extend(arg_partial_tokens)
                if use_arg_start_end:
                    partial_tokens.append(_ARG_END)
            else:
                partial_tokens.extend(arg_partial_tokens)

    return partial_tokens


def nl_to_tokens(s, tokenizer, lemmatization=True):
    """
    Split a natural language string into a sequence of tokens.
    """
    tokens, _ = tokenizer(s, lemmatization=lemmatization)
    return tokens


def cm_to_tokens(s, tokenizer, loose_constraints=True, with_prefix=False,
                 with_suffix=True):
    """
    Split a command string into a sequence of tokens.
    """
    tokens = tokenizer(s, loose_constraints=loose_constraints, 
                       with_prefix=with_prefix, with_suffix=with_suffix)
    return tokens


def tokens_to_ids(tokens, vocabulary):
    """
    Map tokens into their indices in the vocabulary.
    """
    token_ids = []
    for t in tokens:
        if t in vocabulary:
            token_ids.append(vocabulary[t])
        else:
            token_ids.append(UNK_ID)
    return token_ids


def compute_copy_indices(sc_tokens, tg_tokens, tg_vocab, channel):
    csc_ids, ctg_ids = [], []
    init_vocab = CHAR_INIT_VOCAB if channel == 'char' else TOKEN_INIT_VOCAB
    for i, sc_token in enumerate(sc_tokens):
        if (not sc_token in init_vocab) and sc_token in tg_vocab:
            csc_ids.append(tg_vocab[sc_token])
        else:
            csc_ids.append(len(tg_vocab) + sc_tokens.index(sc_token))
    for j, tg_token in enumerate(tg_tokens):
        if tg_token in tg_vocab:
            ctg_ids.append(tg_vocab[tg_token])
        else:
            if tg_token in sc_tokens:
                ctg_ids.append(len(tg_vocab) + sc_tokens.index(tg_token))
            else:
                if channel == 'char':
                    ctg_ids.append(CUNK_ID)
                else:
                    ctg_ids.append(UNK_ID)
    return csc_ids, ctg_ids


def compute_alignments(data_dir, nl_list, cm_list, split, channel):
    alignments = []
    with open(os.path.join(data_dir, '{}.{}.align.readable'.format(split, channel)), 'w') as o_f:
        for nl_tokens, cm_tokens in zip(nl_list, cm_list):
            alignments.append(compute_pair_alignment(nl_tokens, cm_tokens, o_f))
    return alignments


def compute_pair_alignment(nl_tokens, cm_tokens, out_file):
    """
    Compute the alignments between two parallel sequences.
    """
    init_vocab = set(TOKEN_INIT_VOCAB + CHAR_INIT_VOCAB)
    m = len(nl_tokens)
    n = len(cm_tokens)

    A = np.zeros([m, n], dtype=np.int32)

    for i, x in enumerate(nl_tokens):
        for j, y in enumerate(cm_tokens):
            if not x in init_vocab and x == y:
                A[i, j] = 1
                out_file.write('{}-{} '.format(i, j))
    out_file.write('\n')

    return A


def create_vocabulary(vocab_path, dataset, min_word_frequency=1,
                      is_character_model=False):
    """
    Compute the vocabulary of a tokenized dataset and save to file.
    """
    vocab = collections.defaultdict(int)
    for data_point in dataset:
        for token in data_point:
            vocab[token] += 1
    sorted_vocab = [(x, y) for x, y in sorted(vocab.items(), key=lambda x:x[1],
                    reverse=True) if y >= min_word_frequency]
    
    if is_character_model:
        # Character model
        init_vocab = CHAR_INIT_VOCAB
    else:
        init_vocab = TOKEN_INIT_VOCAB
    vocab = [(v, 1000000) for v in init_vocab]
    for v, f in sorted_vocab:
        if not v in init_vocab:
            vocab.append((v, f))

    with open(vocab_path, 'w') as vocab_file:
        for v, f in vocab:
            vocab_file.write('{}\t{}\n'.format(v, f))

    return dict([(x[0], y) for y, x in enumerate(vocab)])


def group_parallel_data(dataset, attribute='source', use_bucket=False,
                        use_temp=False, tokenizer_selector='nl'):
    """
    Group parallel dataset by a certain attribute.

    :param dataset: a list of training quadruples (nl_str, cm_str, nl, cm)
    :param attribute: attribute by which the data is grouped
    :param bucket_input: if the input is grouped in buckets
    :param use_temp: set to true if the dataset is to be grouped by the natural
        language template; false if the dataset is to be grouped by the natural
        language strings
    :param tokenizer_selector: specify which tokenizer to use for making
        templates

    :return: list of (key, data group) tuples sorted by the key value.
    """
    if use_bucket:
        data_points = functools.reduce(lambda x, y: x + y, dataset.data_points)
    else:
        data_points = dataset.data_points

    grouped_dataset = {}
    for i in xrange(len(data_points)):
        data_point = data_points[i]
        attr = data_point.sc_txt \
            if attribute == 'source' else data_point.tg_txt
        if use_temp:
            if tokenizer_selector == 'nl':
                words, _ = tokenizer.ner_tokenizer(attr)
            else:
                words = data_tools.bash_tokenizer(attr, arg_type_only=True)
            temp = ' '.join(words)
        else:
            if tokenizer_selector == 'nl':
                words, _ = tokenizer.basic_tokenizer(attr)
                temp = ' '.join(words)
            else:
                temp = attr
        if temp in grouped_dataset:
            grouped_dataset[temp].append(data_point)
        else:
            grouped_dataset[temp] = [data_point]

    return sorted(grouped_dataset.items(), key=lambda x: x[0])


if __name__ == '__main__':
    print(nl_to_partial_tokens('Execute md5sum command on files found by the find command', tokenizer=tokenizer.basic_tokenizer))
    print(cm_to_partial_tokens("find . -iname \"MyCProgram.c\" -exec md5sum {} \;", tokenizer=data_tools.bash_tokenizer))
