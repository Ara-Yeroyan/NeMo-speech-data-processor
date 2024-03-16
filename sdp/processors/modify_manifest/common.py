# Copyright (c) 2023, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import subprocess
import sys
from functools import partial
from glob import glob
from typing import Callable, Dict, List, Literal, Optional, TypeAlias

from tqdm import tqdm

from sdp.logging import logger
from sdp.processors.base_processor import (
    BaseParallelProcessor,
    BaseProcessor,
    DataEntry,
)

lang_: TypeAlias = Literal['armenian']  # add a couple of your own - Literal["armenian", "new_language"]


class CombineSources(BaseParallelProcessor):
    """Can be used to create a single field from two alternative sources.

    E.g.::

        _target_: sdp.processors.CombineSources
        sources:
            - field: text_pc
              origin_label: original
            - field: text_pc_pred
              origin_label: synthetic
            - field: text
              origin_label: no_pc
        target: text

    will populate the ``text`` field with data from ``text_pc`` field if it's
    present and not equal to ``n/a`` (can be customized). If ``text_pc`` is
    not available, it  will populate ``text`` from ``text_pc_pred`` field,
    following the same rules. If both are not available, it will fall back to
    the ``text`` field itself. In all cases it will specify which source was
    used in the ``text_origin`` field by using the label from the
    ``origin_label`` field.. If non of the sources is available,
    it will populate both the target and the origin fields with ``n/a``.

    Args:
        sources (list[dict]): list of the sources to use in order of preference.
            Each element in the list should be in the following format::

                {
                    field: <which field to take the data from>
                    origin_label: <what to write in the "<target>_origin"
                }
        target (str): target field that we are populating.
        na_indicator (str): if any source field has text equal to the
            ``na_indicator`` it will be considered as not available. If none
            of the sources are present, this will also be used as the value
            for the target and origin fields. Defaults to ``n/a``.

    Returns:
        The same data as in the input manifest enhanced with the following fields::

            <target>: <populated with data from either <source1> or <source2> \
                       or with <na_indicator> if none are available>
            <target>_origin: <label that marks where the data came from>
    """

    def __init__(
        self,
        sources: List[Dict[str, str]],
        target: str,
        na_indicator: str = "n/a",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.sources = sources
        self.target = target
        self.na_indicator = na_indicator

    def process_dataset_entry(self, data_entry: Dict):
        for source_dict in self.sources:
            if data_entry.get(source_dict['field'], self.na_indicator) != self.na_indicator:
                data_entry[self.target] = data_entry[source_dict['field']]
                data_entry[f"{self.target}_origin"] = source_dict['origin_label']
                break  # breaking out on the first present label
        else:  # going here if no break was triggered
            data_entry[self.target] = self.na_indicator
            data_entry[f"{self.target}_origin"] = self.na_indicator

        return [DataEntry(data=data_entry)]


class AddConstantFields(BaseParallelProcessor):
    """This processor adds constant fields to all manifest entries.

    E.g., can be useful to add fixed ``label: <language>`` field for downstream
    language identification model training.

    Args:
        fields: dictionary with any additional information to add. E.g.::

            fields = {
                "label": "en",
                "metadata": "mcv-11.0-2022-09-21",
            }

    Returns:
        The same data as in the input manifest with added fields
        as specified in the ``fields`` input dictionary.
    """

    def __init__(
        self,
        fields: Dict,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.fields = fields

    def process_dataset_entry(self, data_entry: Dict):
        data_entry.update(self.fields)
        return [DataEntry(data=data_entry)]


class DuplicateFields(BaseParallelProcessor):
    """This processor duplicates fields in all manifest entries.

    It is useful for when you want to do downstream processing of a variant
    of the entry. E.g. make a copy of "text" called "text_no_pc", and
    remove punctuation from "text_no_pc" in downstream processors.

    Args:
        duplicate_fields (dict): dictionary where keys are the original
            fields to be copied and their values are the new names of
            the duplicate fields.

    Returns:
        The same data as in the input manifest with duplicated fields
        as specified in the ``duplicate_fields`` input dictionary.
    """

    def __init__(
        self,
        duplicate_fields: Dict,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.duplicate_fields = duplicate_fields

    def process_dataset_entry(self, data_entry: Dict):
        for field_src, field_tgt in self.duplicate_fields.items():
            if not field_src in data_entry:
                raise ValueError(f"Expected field {field_src} in data_entry {data_entry} but there isn't one.")

            data_entry[field_tgt] = data_entry[field_src]

        return [DataEntry(data=data_entry)]


class RenameFields(BaseParallelProcessor):
    """This processor renames fields in all manifest entries.

    Args:
        rename_fields: dictionary where keys are the fields to be
            renamed and their values are the new names of the fields.

    Returns:
        The same data as in the input manifest with renamed fields
        as specified in the ``rename_fields`` input dictionary.
    """

    def __init__(
        self,
        rename_fields: Dict,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.rename_fields = rename_fields

    def process_dataset_entry(self, data_entry: Dict):
        for field_src, field_tgt in self.rename_fields.items():
            if not field_src in data_entry:
                raise ValueError(f"Expected field {field_src} in data_entry {data_entry} but there isn't one.")

            data_entry[field_tgt] = data_entry[field_src]
            del data_entry[field_src]

        return [DataEntry(data=data_entry)]


class SplitOnFixedDuration(BaseParallelProcessor):
    """This processor splits audio into a fixed length segments.

    It does not actually create different audio files, but simply adds
    corresponding ``offset`` and ``duration`` fields. These fields can
    be automatically processed by NeMo to split audio on the fly during
    training.

    Args:
        segment_duration (float): fixed desired duration of each segment.
        drop_last (bool): whether to drop the last segment if total duration is
            not divisible by desired segment duration. If False, the last
            segment will be of a different length which is ``< segment_duration``.
            Defaults to True.
        drop_text (bool): whether to drop text from entries as it is most likely
            inaccurate after the split on duration. Defaults to True.

    Returns:
        The same data as in the input manifest but all audio that's longer
        than the ``segment_duration`` will be duplicated multiple times with
        additional ``offset`` and ``duration`` fields. If ``drop_text=True``
        will also drop ``text`` field from all entries.
    """

    def __init__(
        self,
        segment_duration: float,
        drop_last: bool = True,
        drop_text: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.segment_duration = segment_duration
        self.drop_last = drop_last
        self.drop_text = drop_text

    def process_dataset_entry(self, data_entry: Dict):
        total_duration = data_entry["duration"]
        total_segments = int(total_duration // self.segment_duration)
        output = [None] * total_segments
        for segment_idx in range(total_segments):
            modified_entry = data_entry.copy()  # shallow copy should be good enough
            modified_entry["duration"] = self.segment_duration
            modified_entry["offset"] = segment_idx * self.segment_duration
            if self.drop_text:
                modified_entry.pop("text", None)
            output[segment_idx] = DataEntry(data=modified_entry)

        remainder = total_duration - self.segment_duration * total_segments
        if not self.drop_last and remainder > 0:
            modified_entry = data_entry.copy()
            modified_entry["duration"] = remainder
            modified_entry["offset"] = self.segment_duration * total_segments
            if self.drop_text:
                modified_entry.pop("text", None)
            output.append(DataEntry(data=modified_entry))

        return output


class ChangeToRelativePath(BaseParallelProcessor):
    """This processor changes the audio filepaths to be relative.

    Args:
        base_dir: typically a folder where manifest file is going to be
            stored. All passes will be relative to that folder.

    Returns:
         The same data as in the input manifest with ``audio_filepath`` key
         changed to contain relative path to the ``base_dir``.
    """

    def __init__(
        self,
        base_dir: str,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.base_dir = base_dir

    def process_dataset_entry(self, data_entry: Dict):
        data_entry["audio_filepath"] = os.path.relpath(data_entry["audio_filepath"], self.base_dir)

        return [DataEntry(data=data_entry)]


class SortManifest(BaseProcessor):
    """Processor which will sort the manifest by some specified attribute.

    Args:
        attribute_sort_by (str): the attribute by which the manifest will be sorted.
        descending (bool): if set to False, attribute will be in ascending order.
            If True, attribute will be in descending order. Defaults to True.

    Returns:
        The same entries as in the input manifest, but sorted based
        on the provided parameters.
    """

    def __init__(
        self,
        attribute_sort_by: str,
        descending: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.attribute_sort_by = attribute_sort_by
        self.descending = descending

    def process(self):
        with open(self.input_manifest_file, "rt", encoding="utf8") as fin:
            dataset_entries = [json.loads(line) for line in fin.readlines()]

        dataset_entries = sorted(dataset_entries, key=lambda x: x[self.attribute_sort_by], reverse=self.descending)

        with open(self.output_manifest_file, "wt", encoding="utf8") as fout:
            for line in dataset_entries:
                fout.write(json.dumps(line, ensure_ascii=False) + "\n")


class KeepOnlySpecifiedFields(BaseProcessor):
    """Saves a copy of a manifest but only with a subset of the fields.

    Typically will be the final processor to save only relevant fields
    in the desired location.

    Args:
        fields_to_keep (list[str]): list of the fields in the input manifest
            that we want to retain. The output file will only contain these
            fields.

    Returns:
        The same data as in input manifest, but re-saved in the new location
        with only ``fields_to_keep`` fields retained.
    """

    def __init__(self, fields_to_keep: List[str], **kwargs):
        super().__init__(**kwargs)
        self.fields_to_keep = fields_to_keep

    def process(self):
        with open(self.input_manifest_file, "rt", encoding="utf8") as fin, open(
            self.output_manifest_file, "wt", encoding="utf8"
        ) as fout:
            for line in tqdm(fin):
                line = json.loads(line)
                new_line = {field: line[field] for field in self.fields_to_keep}
                fout.write(json.dumps(new_line, ensure_ascii=False) + "\n")


class RemoveExtraSymbols(BaseParallelProcessor):
    """Removes extra (defined manually) symbols instead of `Hard` dropping the whole sentence
    as the data_to_dropbool.py Processors do (e.g. DropNonAlphabet, DropIfNoneOfRegexMatch ...)

    Args:
        ignore_symbols (str): a string containing all of the characters/symbols to be deleted
        target_language (str): the language of the dataset (can be used to define specific rules)
        text_key (str): a string indicating which key of the data entries
        should be used to find the utterance transcript. Defaults to "text".

            .. note::
                Don't forget to keep the have the punctuations in the target_language
                instead of their English equivalents

    Returns:
         The same data as in the input manifest with some text modifications (removed extra symbols).
    """

    def __init__(
        self,
        ignore_symbols: str,
        target_language: str,
        text_key: str = "text",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.text_key = text_key
        self.filter_text = partial(self.clear_text, ignore=ignore_symbols, lang=target_language)

    @staticmethod
    def clear_text(text: str, ignore: str, lang: Optional[lang_] = 'armenian') -> str:
        """
        Function that iterates over a string (text) and removes extra symbols (replaces with '')
        The utility is static method as it can also be used for instance during CreateInitialManifestMCV

        Args:
             text (str): the sentence to be processed - text.replace("<extra_symbols>", "")
             ignore (str): a string containing all of the characters/symbols to be deleted from text
        Returns:
            Cleaned text (str)
        """
        if lang == 'armenian':
            text = text.replace(".", "․").replace(":", "։")

        for x in ignore:
            text = text.replace(x, "")
            text = text.replace(x.upper(), "")

        return text

    def process_dataset_entry(self, data_entry) -> List:
        original_text = data_entry[self.text_key]
        cleaned_text = self.filter_text(original_text)
        n_removed_symbols = len(original_text) - len(cleaned_text)
        return [DataEntry(data=data_entry, metrics=n_removed_symbols)]

    def finalize(self, metrics):
        logger.info(f"Num of extra symbols removed: {sum(metrics)}")
        super().finalize(metrics)


class CreateTokenizer(BaseProcessor):
    """Processor which will sort the manifest by some specified attribute.

    Args:
        nemo_repo_path (str): path to cloned NeMo repo directory.
        vocab_size (int): vocabular size used in encoding the text.
        data_folder (str): path to folder where .tsv files are stored.
        nemo_executable (str): as python virtualEnvs can differ from nemo & nemo-SDP, provide the python.exe manually

        extra_corpus (str): `glob` path to extra .txts to build the tokenizer.
        target_language (str): the language of the dataset (can be used to define specific rules).
        ignore_symbols (str): a string containing all of the characters/symbols to be deleted (when inserting extra_corpus data).

        tokenizer (Literal["spe", "bpe"]): type of tokenization to perform - wpe or spe.
        lower_case (bool): whether to tokenize with lower case character set only (for english).
        spe_type: (Literal['bpe', 'unigram', 'char', 'word']): type of tokenization model used for spe.

    Returns:
        None
                .. note::
                    Creates directory where stores the corpus (document.txt) the tokenizer (.pb) and the vocab
    """

    def __init__(
        self,
        data_folder: str,
        nemo_repo_path: str,
        vocab_size: int = 128,
        extra_corpus: str = '',
        lower_case: bool = True,
        ignore_symbols: str = '',
        target_language: str = '',
        nemo_executable: str = '~/anaconda3/envs/nemo/python.exe',
        tokenizer: Literal['spe', 'bpe'] = 'spe',
        spe_type: Literal['bpe', 'unigram', 'char', 'word'] = 'unigram',
        **kwargs,
    ):
        super().__init__(**kwargs)
        script_path = os.path.join(nemo_repo_path, 'scripts/tokenizers/process_asr_text_tokenizer.py')

        if not os.path.exists(nemo_executable):
            logger.warning(
                f"Provided nemo executable: {nemo_executable} does not exists. Have to replace with {sys.executable}"
            )
            nemo_executable = sys.executable

        def get_command_to_run() -> List[str]:
            """
            Storing the procedure for preparing the tokenizer training
            No files (text_corpus, document.txt) are created until self.process()
            """
            os.makedirs(os.path.join(data_folder, 'text_corpus'), exist_ok=True)
            document_path = os.path.join(data_folder, 'text_corpus', 'document.txt')

            with open(document_path, 'w', encoding='utf-8') as out_writer:
                with open(self.input_manifest_file, 'r', encoding='utf-8') as in_reader:
                    for line in in_reader:
                        item = json.loads(line)
                        text = item['text']

                        out_writer.write(text + '\n')
                        out_writer.flush()

            logger.info(f"Finished extracting manifest from MCV: {document_path}")

            if extra_corpus:
                filter_text = partial(RemoveExtraSymbols.clear_text, ignore=ignore_symbols, lang=target_language)
                self.insert_files_into_doc(document_path, extra_corpus, filter_text)

            command = [
                nemo_executable,
                script_path,
                '--data_file',
                document_path,
                "--vocab_size",
                vocab_size,
                "--data_root",
                data_folder,
                "--tokenizer",
                tokenizer,
                "--spe_type",
                spe_type,
            ]
            if lower_case:
                command.append("--no_lower_case")

            return [str(x) for x in command]

        self.get_command = get_command_to_run

    @staticmethod
    def insert_files_into_doc(doc_txt_path: str, corpus: str, filter_function: Callable = lambda x: x) -> None:
        corpus = glob(corpus)
        assert isinstance(corpus, list)

        with open(doc_txt_path, 'a', encoding='utf-8') as doc_file:
            for txt_file_path in tqdm(corpus):
                with open(txt_file_path, 'r', encoding='utf-8') as txt_file:
                    for line in txt_file:
                        doc_file.write(filter_function(line))

    def process(self):
        subprocess.run(self.get_command())


class ConvertDatasetToTar(BaseProcessor):
    """Processor which will sort the manifest by some specified attribute.

    Args:
        workers (int): Number of worker processes.
        nemo_repo_path (str): path to cloned NeMo repo directory.
        nemo_executable (str): as python virtualEnvs can differ from nemo & nemo-SDP, provide the python.exe manually
        sort_in_shards (bool): Whether or not to sort samples inside the shards based on their duration.
        num_shards (int): Number of shards (tarballs) to create. Used for partitioning data among workers.
        shuffle (bool): Whether or not to randomly shuffle the samples in the manifest before tarring/sharding.
        max_duration (float): Maximum duration of audio clip in the dataset. By default, it is None and is required to be set.
        min_duration (float): Minimum duration of audio clip in the dataset. By default, it is None and will not filter files.
        target_dir (str): Target directory for resulting tarballs and manifest. Defaults to `./tarred`. Creates the path if necessary.

    Returns:
        None
                .. note::
                    Creates directory where stores the corpus (document.txt) the tokenizer (.pb) and the vocab
    """

    def __init__(
        self,
        target_dir: str,
        nemo_repo_path: str,
        shuffle: bool = True,
        workers: int = -1,
        nemo_executable: str = "~/anaconda3/envs/nemo/python.exe",
        shuffle_seed: int = 1,
        num_shards: int = 1024,
        min_duration: float = 1.0,
        max_duration: float = 15.0,
        sort_in_shards: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        script_path = os.path.join(nemo_repo_path, 'scripts\speech_recognition\convert_to_tarred_audio_dataset.py')

        if not os.path.exists(nemo_executable):
            nemo_executable = sys.executable

        def get_command_to_run() -> List[str]:
            """
            Storing the procedure for preparing the tokenizer training
            No files (text_corpus, document.txt) are created until self.process()
            """
            command = [
                nemo_executable,
                script_path,
                '--manifest_path',
                self.input_manifest_file,
                "--min_duration",
                min_duration,
                "--target_dir",
                target_dir,
                "--shuffle_seed",
                shuffle_seed,
                "--max_duration",
                max_duration,
                "--workers",
                workers,
                "--num_shards",
                num_shards,
            ]
            if shuffle:
                command.append("--shuffle")

            if sort_in_shards:
                command.append("--sort_in_shards")

            return [str(x) for x in command]

        self.get_command = get_command_to_run

    @staticmethod
    def insert_files_into_doc(doc_txt_path: str, corpus: str, filter_function: Callable = lambda x: x) -> None:
        corpus = glob(corpus)
        assert isinstance(corpus, list)

        with open(doc_txt_path, 'a', encoding='utf-8') as doc_file:
            for txt_file_path in tqdm(corpus):
                with open(txt_file_path, 'r', encoding='utf-8') as txt_file:
                    for line in txt_file:
                        doc_file.write(filter_function(line))

    def process(self):
        subprocess.run(self.get_command())
