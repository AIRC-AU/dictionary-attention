from sacremoses import MosesTokenizer, MosesDetokenizer
from subword_nmt.apply_bpe import BPE
from model import Model
from decode import triu_mask
from io import StringIO
import torch, json, math, re
import torch.nn as nn

class Vocab:

    def __init__(self, vocab_file=None):
        self.num_to_word = []
        self.word_to_num = {}
        self._decoding_size = None
        if vocab_file:
            self._from_file(vocab_file)

    def _from_file(self, vocab_file):
        line = vocab_file.readline()
        start, end = line.lstrip('#').split(':')
        decoding_size = int(end) - int(start)

        for i in range(8 - decoding_size % 8):
            self.add(f'<PAD {i}>')
        decoding_size += i + 1 if i else 0
        for line in vocab_file:
            self.add(line.split()[0])
        for j in range(8 - self.size() % 8):
            self.add(f'<PAD {i + j}>')

        self._decoding_size = decoding_size

    @property
    def UNK(self):
        try:
            return self.word_to_num['<UNK>']
        except ValueError:
            self.add('<UNK>')
        return self.word_to_num['<UNK>']

    @property
    def BOS(self):
        try:
            return self.word_to_num['<BOS>']
        except ValueError:
            self.add('<BOS>')
        return self.word_to_num['<BOS>']

    @property
    def EOS(self):
        try:
            return self.word_to_num['<EOS>']
        except ValueError:
            self.add('<EOS>')
        return self.word_to_num['<EOS>']

    @property
    def PAD(self):
        try:
            return self.word_to_num['<PAD>']
        except ValueError:
            self.add('<PAD>')
        return self.word_to_num['<PAD>']

    def add(self, word):
        try:
            self.word_to_num[word] = self.size()
        except ValueError:
            pass
        else:
            self.num_to_word.append(word)      

    def remove(self, word):
        try:
            self.word_to_num.pop(word)
        except KeyError:
            pass
        else:
            self.num_to_word.remove(word)

    def numberize(self, *words, as_list=False):
        nums = []
        for word in words:
            try:
                nums.append(self.word_to_num[word])
            except KeyError:
                nums.append(self.UNK)
        return nums if as_list else torch.tensor(nums)

    def denumberize(self, *nums, verbatim=False):
        if verbatim:
            return [self.num_to_word[num] for num in nums]
        try:
            start = nums.index(self.BOS) + 1
        except ValueError:
            start = 0
        try:
            end = nums.index(self.EOS)
        except ValueError:
            end = len(nums)
        return [self.num_to_word[num] for num in nums[start:end]]

    def size(self, decoding=False):
        if decoding:
            return self._decoding_size
        return len(self.num_to_word)

class Batch:

    def __init__(self, src_nums, tgt_nums, dict_data, device=None, ignore_index=None):
        self._src_nums = src_nums
        self._tgt_nums = tgt_nums
        self._dict_data = dict_data
        self.device = device
        self.ignore_index = ignore_index

    @property
    def src_nums(self):
        return self._src_nums.to(self.device)

    @property
    def tgt_nums(self):
        return self._tgt_nums.to(self.device)

    @property
    def src_mask(self):
        if not self.ignore_index:
            return None
        return (self.src_nums != self.ignore_index).unsqueeze(-2)

    @property
    def tgt_mask(self):
        if not self.ignore_index:
            return triu_mask(self.tgt_nums[:, :-1].size(-1))
        return (self.tgt_nums[:, :-1] != self.ignore_index).unsqueeze(-2) \
            & triu_mask(self.tgt_nums[:, :-1].size(-1), device=self.device)

    @property
    def dict_mask(self):
        dict_mask = torch.zeros(self.src_mask.size(), device=self.device) \
            .repeat((2, 1, self.src_mask.size(-1), 1))
        for i, (lemmas, senses) in enumerate(self._dict_data):
            for (a, b), (c, d) in zip(lemmas, senses):
                # only lemmas can attend to their senses
                dict_mask[0, i, :, c:d] = 1.
                dict_mask[0, i, a:b, c:d] = 0.
                # senses can only attend to themselves
                dict_mask[1, i, c:d, :] = 1.
                dict_mask[1, i, c:d, c:d] = 0.
        return dict_mask

    @property
    def num_tokens(self):
        if not self.ignore_index:
            return self.tgt_nums[:, 1:].sum()
        return (self.tgt_nums[:, 1:] != self.ignore_index).sum()

    def size(self):
        return self._src_nums.size(0)

class Manager:

    def __init__(self, src_lang, tgt_lang, vocab_file, codes_file, model_file,
            config, device, dict_file, freq_file, data_file=None, test_file=None):
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self._model_file = model_file
        self._vocab_file = vocab_file
        self._codes_file = codes_file
        self.config = config
        self.device = device

        for option, value in config.items():
            self.__setattr__(option, value)

        if not isinstance(vocab_file, str):
            vocab_file.seek(0)
            self._vocab_file = ''.join(vocab_file.readlines())
        self.vocab = Vocab(StringIO(self._vocab_file))

        if not isinstance(codes_file, str):
            codes_file.seek(0)
            self._codes_file = ''.join(codes_file.readlines())
        self.codes = BPE(StringIO(self._codes_file))

        self.model = Model(
            self.vocab.size(),
            self.embed_dim,
            self.ff_dim,
            self.num_heads,
            self.num_layers,
            self.dropout
        ).to(device)

        dict_file.seek(0)
        self.dict = json.load(dict_file)
        assert len(self.dict) > 0

        freq_file.seek(0)
        self.freq = {}
        for line in freq_file:
            word, freq = line.split()
            self.freq[word] = int(freq)
        assert len(self.freq) > 0

        if data_file:
            data_file.seek(0)
            self.data = self.batch_data(data_file)
            assert len(self.data) > 0
        else:
            self.data = None

        if test_file:
            test_file.seek(0)
            self.test = self.batch_data(test_file)
            assert len(self.test) > 0
        else:
            self.test = None

    def tokenize(self, string, lang=None):
        if lang is None:
            lang = self.src_lang
        tokens = MosesTokenizer(lang).tokenize(string)
        return self.codes.process_line(' '.join(tokens))

    def detokenize(self, tokens, lang=None):
        if lang is None:
            lang = self.tgt_lang
        string = MosesDetokenizer(lang).detokenize(tokens)
        return re.sub('(@@ )|(@@ ?$)', '', string)

    def save_model(self):
        torch.save({
            'state_dict': self.model.state_dict(),
            'src_lang': self.src_lang,
            'tgt_lang': self.tgt_lang,
            'vocab_file': self._vocab_file,
            'codes_file': self._codes_file,
            'config': self.config
        }, self._model_file)

    def batch_data(self, data_file):
        unbatched, batched = [], []
        for line in data_file:
            src_line, tgt_line = line.split('\t')
            if not src_line or not tgt_line:
                continue
            src_words = ['<BOS>'] + src_line.split() + ['<EOS>']
            tgt_words = ['<BOS>'] + tgt_line.split() + ['<EOS>']
            if len(src_words) > self.max_length:
                continue
            if len(tgt_words) > self.max_length:
                continue

            lemmas, senses = [], []
            i, src_len = -1, len(src_words)
            while (i := i + 1) < src_len:
                lemma_start = i
                if src_words[i].endswith('@@'):
                    lemma = src_words[i].rstrip('@@')
                    while (i := i + 1) < src_len and src_words[i].endswith('@@'):
                        lemma += src_words[i].rstrip('@@')
                    lemma += src_words[i]
                else:
                    lemma = src_words[i]
                lemma_end = i + 1

                if lemma in self.freq and lemma in self.dict:
                    if self.freq[lemma] <= self.freq_limit:
                        sense_start = len(src_words)
                        sense = self.dict[lemma][:self.max_senses]
                        sense_end = sense_start + len(sense)

                        lemmas.append((lemma_start, lemma_end))
                        senses.append((sense_start, sense_end))

                        if len(src_words) + len(sense) > self.max_length:
                            lemmas.pop(-1)
                            senses.pop(-1)
                            break
                        src_words.extend(sense)

            unbatched.append((src_words, tgt_words, lemmas, senses))

        unbatched.sort(key=lambda x: (len(x[0]), len(x[1])), reverse=True)

        i = batch_size = 0
        while (i := i + batch_size) < len(unbatched):
            src_len = len(unbatched[i][0])
            tgt_len = len(unbatched[i][1])

            while True:
                batch_size = self.batch_size // (max(src_len, tgt_len) * 8) * 8
    
                src_batch, tgt_batch, lemmas, senses = zip(*unbatched[i:(i + batch_size)])
                max_src_len = max(len(src_words) for src_words in src_batch)
                max_tgt_len = max(len(tgt_words) for tgt_words in tgt_batch)

                if src_len >= max_src_len and tgt_len >= max_tgt_len: break
                src_len, tgt_len = max_src_len, max_tgt_len

            max_src_len = math.ceil(max_src_len / 8) * 8
            max_tgt_len = math.ceil(max_tgt_len / 8) * 8

            src_nums = torch.stack([
                nn.functional.pad(self.vocab.numberize(*src_words), (0, max_src_len - len(src_words)),
                    value=self.vocab.PAD) for src_words in src_batch])
            tgt_nums = torch.stack([
                nn.functional.pad(self.vocab.numberize(*tgt_words), (0, max_tgt_len - len(tgt_words)),
                    value=self.vocab.PAD) for tgt_words in tgt_batch])
            tgt_nums.masked_fill_(tgt_nums >= self.vocab.size(decoding=True), self.vocab.UNK)

            batched.append(Batch(src_nums, tgt_nums, zip(lemmas, senses), self.device, self.vocab.PAD))

        return batched
