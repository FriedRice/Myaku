"""Utilities for analyzing Japanese text."""

import logging
import os
import re
import shelve
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Tuple, Union
from xml.etree import ElementTree

import MeCab

import myaku
from myaku import utils
from myaku.datatypes import (
    ArticleTextPosition,
    FoundJpnLexicalItem,
    InterpSource,
    JpnArticle,
    JpnLexicalItemInterp,
    MecabLexicalItemInterp,
    reduce_found_lexical_items,
)
from myaku.errors import (
    ResourceLoadError,
    ResourceNotReadyError,
    TextAnalysisError,
)

_log = logging.getLogger(__name__)

_JMDICT_XML_FILEPATH_ENV_VAR = 'JMDICT_XML_FILEPATH'
_JMDICT_FILE_VERSION_REGEX = re.compile(
    r'^<!-- JMdict created: (\d\d\d\d)-(\d\d)-(\d\d) -->$'
)

_IPADIC_NEOLOGD_GIT_DIR_ENV_VAR = 'IPADIC_NEOLOGD_GIT_DIR'
_IPADIC_NEOLOGD_CHANGELOG_FILENAME = 'ChangeLog'
_IPADIC_NEOLOGD_VERSION_REGEX = re.compile(
    r'^# Release (\d\d\d\d)(\d\d)(\d\d)-.*$'
)

MecabTags = Tuple[str, ...]


def get_resource_version_info() -> Dict[str, str]:
    """Return the version info of the resources used by this module.

    Includes versions of resources such as Japanese dictionaries and
    morphological analyzers used by this module.

    Returns:
        Dictionary where the keys are the names of resources used by this
        module and the values are the versions of those resources currently
        being used by the module.
    """
    version_info = {}
    version_info['MeCab'] = _get_mecab_version()
    version_info['JMdict'] = _get_jmdict_version()
    version_info['ipadic-NEologd'] = _get_ipadic_neologd_version()

    return version_info


def _get_mecab_version() -> str:
    """Return version of MeCab on the system."""
    output = subprocess.run(
        ['mecab-config', '--version'], capture_output=True
    )
    if output.returncode != 0:
        utils.log_and_raise(
            _log, ResourceLoadError,
            'mecab is not available on this system'
        )

    mecab_version = output.stdout.decode(sys.stdout.encoding).strip()
    return mecab_version


def _get_jmdict_version() -> str:
    """Return version of JMdict currently used by this module.

    The version for JMdict will be in the form of a date "yyyy.mm.dd". For
    example, 2019.06.11 for the JMdict generated on June 11th, 2019.
    """
    jmdict_xml_filepath = utils.get_value_from_env_variable(
        _JMDICT_XML_FILEPATH_ENV_VAR
    )

    if not os.path.exists(jmdict_xml_filepath):
        utils.log_and_raise(
            _log, ResourceLoadError,
            'JMdict XML file not found at "{}"'.format(jmdict_xml_filepath)
        )

    match = None
    with open(jmdict_xml_filepath, 'r') as jmdict_file:
        for line in jmdict_file:
            match = re.match(_JMDICT_FILE_VERSION_REGEX, line)
            if match is not None:
                break

    if match is None:
        utils.log_and_raise(
            _log, ResourceLoadError,
            'JMdict XML file at "{}" does not contain version info'.format(
                jmdict_xml_filepath
            )
        )

    return '{}.{}.{}'.format(match.group(1), match.group(2), match.group(3))


def _get_ipadic_neologd_version() -> str:
    """Return version of ipadic-NEologd used by this module.

    The version for ipadic-NEologd will be in the form of a date "yyyy.mm.dd".
    For example, 2019.06.11 for the ipadic-NEologd generated on June 11th,
    2019.
    """
    git_dir = utils.get_value_from_env_variable(
        _IPADIC_NEOLOGD_GIT_DIR_ENV_VAR
    )
    changelog_path = os.path.join(git_dir, _IPADIC_NEOLOGD_CHANGELOG_FILENAME)

    if not os.path.exists(changelog_path):
        utils.log_and_raise(
            _log, ResourceLoadError,
            'ipadic-NEologd change log file not found at "{}"'.format(
                changelog_path
            )
        )

    match = None
    with open(changelog_path, 'r') as changelog_file:
        for line in changelog_file:
            match = re.match(_IPADIC_NEOLOGD_VERSION_REGEX, line)
            if match is not None:
                break

    if match is None:
        utils.log_and_raise(
            _log, ResourceLoadError,
            'ipadic-NEologd change log file at "{}" does not contain verison '
            'info'.format(changelog_path)
        )

    return '{}.{}.{}'.format(match.group(1), match.group(2), match.group(3))


@utils.singleton_per_config
class JapaneseTextAnalyzer(object):
    """Analyzer for Japanese text to determine used lexical items."""

    _SYMBOL_PART_OF_SPEECH = '記号'  # Japanese word for symbol (kigou)

    def __init__(self) -> None:
        """Load the external resources needed for text analysis."""
        jmdict_xml_filepath = utils.get_value_from_env_variable(
            _JMDICT_XML_FILEPATH_ENV_VAR
        )
        self._jmdict = JMdict(
            jmdict_xml_filepath
        )

        self._mecab_tagger = MecabTagger()

    @utils.add_debug_logging
    def find_article_lexical_items(
        self, article: JpnArticle
    ) -> List[FoundJpnLexicalItem]:
        """Find all Japanese lexical items in an article.

        Args:
            article: Japnaese article whose full_text will be analyzed to find
                lexical items.

        Returns:
            A list of all of the found lexical items in the article.
        """
        article_blocks = article.full_text.splitlines()
        _log.debug(
            'Article "%s" split into %s blocks',
            article, len([b for b in article_blocks if len(b) > 0])
        )

        offset = 0
        article_lexical_items = []
        for text_block in article_blocks:
            if len(text_block) == 0:
                offset += 1  # for new line char
                continue

            found_lexical_items = self._find_lexical_items(
                text_block, offset, article
            )

            _log.debug(
                'Found %s lexical items in block "%s"',
                len(found_lexical_items), utils.shorten_repr(text_block, 15)
            )
            article_lexical_items.extend(found_lexical_items)

            offset += len(text_block) + 1  # +1 for new line char

        reduced_flis = reduce_found_lexical_items(article_lexical_items)
        return reduced_flis

    def _find_lexical_items(
        self, text: str, offset: int, article: JpnArticle
    ) -> List[FoundJpnLexicalItem]:
        """Find all Japanese lexical items in a block of text.

        Args:
            text: Text block that will be analyzed for lexical items.
            offset: The character offset of the text block in its article.
            article: The article containing the text block.

        Returns:
            The found Japanese lexical items in the text.
        """
        mecab_lexical_items = self._mecab_tagger.parse(text, offset)
        for lexical_item in mecab_lexical_items:
            lexical_item.article = article

        # MeCab generally parses text into base lexical items while ignoring
        # meta lexical items, so meta lexical items must be found separately.
        meta_lexical_items = self._find_meta_lexical_items(mecab_lexical_items)

        found_lexical_items = mecab_lexical_items + meta_lexical_items
        processed_lexical_items = []
        for item in found_lexical_items:
            # Mecab includes symbols such as periods and commas in the output
            # of its parse. These aren't really lexical items, and they aren't
            # really useful, so they are discarded here.
            if self._is_symbol(item):
                continue

            item.article = article
            processed_lexical_items.append(item)

        return processed_lexical_items

    def _find_meta_lexical_items(
        self, base_lexical_items: List[FoundJpnLexicalItem]
    ) -> List[FoundJpnLexicalItem]:
        """Find the meta lexical items within a series of base lexical items.

        Base lexical items are lexical items that cannot be subdivided into
        multiple lexical items. Meta lexical items are lexical items that
        consist of two or more base lexical items.

        Args:
            base_lexical_items: A series of base lexical items in the same
                order that they were in the text they were found in.

        Returns:
            A list of the meta lexical items found within the given base
            lexical items.
        """
        start = 0
        meta_lexical_items: List[FoundJpnLexicalItem] = []

        while start < len(base_lexical_items):
            end = start + 1
            while end < len(base_lexical_items):
                decomp = base_lexical_items[start:end + 1]
                if not self._within_jmdict_max_entry_len(decomp):
                    break

                lookup_lexical_items = self._lookup_meta_lexical_item(decomp)
                meta_lexical_items.extend(lookup_lexical_items)
                end += 1
            start += 1

        return meta_lexical_items

    @utils.skip_method_debug_logging
    def _within_jmdict_max_entry_len(
        self, flis: List[FoundJpnLexicalItem]
    ) -> bool:
        """Check if a lexical item series len is <= max JMdict entry len.

        There are several ways to measure the length of a lexical item series
        (i.e. # of items, len of surface forms, len of base forms), so this
        function checks if any of those measures results in a length less than
        the maximum JMdict entry length for that measure.

        Args:
            flis: A series of lexical items to check if <= max JMdict entry
                len.

        Returns:
            True if at least one measure of the len of the lexical item series
            is less than the max JMdict entry length for that measure, and
            False if otherwise.
        """
        if len(flis) <= self._jmdict.max_mecab_decomp_len:
            return True

        base_form_len = sum(len(item.base_form) for item in flis)
        if base_form_len <= self._jmdict.max_text_form_len:
            return True

        surface_form_len = sum(
            len(item.get_first_surface_form()) for item in flis
        )
        if surface_form_len <= self._jmdict.max_text_form_len:
            return True

        return False

    @utils.skip_method_debug_logging
    def _lookup_meta_lexical_item(
        self, base_decomp: List[FoundJpnLexicalItem]
    ) -> List[FoundJpnLexicalItem]:
        """Look up the meta lexical item in JMdict.

        Args:
            base_decomp: The decomposition of the meta lexical item into base
                lexical items.

        Returns:
            A list of all of the lexical items found in JMdict that match the
            meta lexical item.
        """
        decomp_base_forms = tuple(item.base_form for item in base_decomp)
        decomp_entries = self._jmdict[decomp_base_forms]

        surface_form = ''.join(
            item.get_first_surface_form() for item in base_decomp
        )
        surface_entries = self._jmdict[surface_form]

        base_form = ''.join(decomp_base_forms)
        base_entries = self._jmdict[base_form]
        if not (decomp_entries or surface_entries or base_entries):
            return []

        lexical_items = []
        entries = utils.unique(decomp_entries + surface_entries + base_entries)
        for entry in entries:
            sources = []
            if entry in decomp_entries:
                sources.append(InterpSource.JMDICT_MECAB_DECOMP)
            if entry in surface_entries:
                sources.append(InterpSource.JMDICT_SURFACE_FORM)
            if entry in base_entries:
                sources.append(InterpSource.JMDICT_BASE_FORM)

            lexical_item = FoundJpnLexicalItem(
                base_form=entry.text_form,
                found_positions=[ArticleTextPosition(
                    base_decomp[0].found_positions[0].start, len(surface_form)
                )],
                possible_interps=[
                    JpnLexicalItemInterp(
                        jmdict_interp_entry_id=entry.entry_id,
                        interp_sources=tuple(sources)
                    )
                ]
            )
            lexical_items.append(lexical_item)

        return lexical_items

    @utils.skip_method_debug_logging
    def _is_symbol(self, fli: FoundJpnLexicalItem) -> bool:
        """Return True if the Japanese lexical item is a non-alnum symbol.

        Symbols include things like periods, commas, quote characters, etc.
        """
        for possible_interp in fli.possible_interps:
            if possible_interp.mecab_interp is None:
                continue

            for part_of_speech in possible_interp.mecab_interp.parts_of_speech:
                if part_of_speech == self._SYMBOL_PART_OF_SPEECH:
                    return True
        return False


@dataclass
class JMdictEntry(object):
    """The data for an entry from JMdict.

    This class does NOT map exactly to the format official JMdict XML uses to
    store entries. A proper entry from JMdict XML contains all text form
    representations (readings and writings) of an entry, but this class holds
    only a single text form from an entry and the subset of info from that
    entry related to that text form. This form is used because it is easier to
    work with for text analysis.

    Attributes:
        entry_id: The unique ID of the JMdict XML entry that the data for this
            entry came from.
        text_form: The Japanese text representation of the entry. The defining
            part of the entry.
        text_form_info: Info related to this specific text form that may not
            apply to other text forms of the same entry (e.g. if the text form
            uses ateji kanji).
        text_form_freq: Info related to how frequently this entry is used in
            Japanese. See JMdict schema for how to decode this info.
        parts_of_speech: Parts of speech that apply to this entry.
        fields: The fields of application for this entry (e.g. food term,
            baseball term, etc.)
        dialect: The dialects that apply for this entry (e.g. kansaiben).
        misc: Other miscellaneous info recorded for this entry from JMdict.
    """
    entry_id: str = None
    text_form: str = None
    text_form_info: Tuple[str, ...] = None
    text_form_freq: Tuple[str, ...] = None
    parts_of_speech: Tuple[str, ...] = None
    fields: Tuple[str, ...] = None
    dialects: Tuple[str, ...] = None
    misc: Tuple[str, ...] = None


@utils.singleton_per_config
@utils.add_method_debug_logging
class JMdict(object):
    """Object representation of a JMdict dictionary."""

    EntryMap = Dict[str, List[JMdictEntry]]
    MecabDecompMap = Dict[Tuple[str, ...], List[JMdictEntry]]

    _SHELF_FILENAME = 'JMdict.shelf'

    _REPR_ELEMENT_TAGS = {
        'k_ele',  # Kanji representation
        'r_ele',  # Reading (kana) representation
    }

    _SENSE_ELEMENT_TAG = 'sense'

    _ENTRY_ID_TAG = 'ent_seq'

    _REPR_TEXT_FORM_TAG = {
        'k_ele': 'keb',
        'r_ele': 'reb',
    }

    _REPR_OPTIONAL_TAGS = {
        'k_ele': [
            'ke_inf',  # Text form information
            'ke_pri',  # Text form frequency
        ],
        'r_ele': [
            're_inf',
            're_pri',
        ],
    }

    _SENSE_OPTIONAL_TAGS = {
        'stagk',  # Applicable kanji representation
        'stagr',  # Applicable reading (kana) representation
        'pos',  # Part of speech
        'field',  # Field of application (e.g. food, baseball, etc.)
        'misc',  # Categorized extra info
        'dial',  # Dialect
        's_inf',  # Uncategorized extra info
    }

    _TAG_TO_OBJ_ATTR_MAP = {
        'ent_seq': 'entry_id',
        'keb': 'text_form',
        'reb': 'text_form',
        'ke_inf': 'text_form_info',
        're_inf': 'text_form_info',
        'ke_pri': 'text_form_freq',
        're_pri': 'text_form_freq',
        'stagk': 'applicable_elements',
        'stagr': 'applicable_elements',
        'pos': 'parts_of_speech',
        'field': 'fields',
        'misc': 'misc',
        'dial': 'dialects',
        's_inf': 'misc',
    }

    # These tags can have more than one element per entry, so their info should
    # be stored in a tuple of strings rather than a single string.
    _TUPPLE_TAGS = {
        'ke_inf',
        're_inf',
        'ke_pri',
        're_pri',
        'stagk',
        'stagr',
        'pos',
        'field',
        'misc',
        'dial',
        's_inf',
    }

    @property
    def max_text_form_len(self) -> int:
        """Max len of a text form of the loaded JMdict entries.

        Property in order to make it read-only.
        """
        if self._max_text_form_len is None:
            utils.log_and_raise(
                _log, ResourceNotReadyError,
                'JMdict object used before loading any JMdict data.'
            )

        return self._max_text_form_len

    @property
    def max_mecab_decomp_len(self) -> int:
        """Max len of a MeCab decomposition of the loaded JMdict entries.

        Property in order to make it read-only.
        """
        if self._max_mecab_decomp_len is None:
            utils.log_and_raise(
                _log, ResourceNotReadyError,
                'JMdict object used before loading any JMdict data.'
            )

        return self._max_mecab_decomp_len

    @dataclass
    class _JMdictSense(object):
        """The data for a sense element for a JMdict entry.

        A sense of a JMdict entry holds various info about the entry that can
        apply to some or all of the representational elements of the entry.

        This class is only used during processing internal to the JMdict class.
        This information is then exposed publicly via the JMdictEntry class.

        Attributes:
            applicable_reprs: Tuple of representations of the entry that this
                sense applys to. If empty, applies to all reprs of the entry.
            parts_of_speech: Parts of speech that the entry can be.
            fields: The fields of application for this entry (e.g. food term,
                baseball term, etc.)
            dialect: The dialects that apply for this entry (e.g. kansaiben).
            misc: Other miscellaneous info recorded for this entry in JMdict.
        """
        applicable_elements: Tuple[str, ...] = None
        parts_of_speech: Tuple[str, ...] = None
        fields: Tuple[str, ...] = None
        dialects: Tuple[str, ...] = None
        misc: Tuple[str, ...] = None

    def __init__(self, jmdict_xml_filepath: str = None) -> None:
        """Initialize the JMdict dictionary lookup data structures.

        Args:
            jmdict_xml_filepath: JMdict XML file to load the JMdict data from.
        """
        self._entry_map: JMdict.EntryMap = None
        self._mecab_decomp_map: JMdict.MecabDecompMap = None
        self._max_text_form_len: int = None
        self._max_mecab_decomp_len: int = None
        self._mecab_tagger = MecabTagger()

        if jmdict_xml_filepath is not None:
            self.load_jmdict(jmdict_xml_filepath)

    @utils.skip_method_debug_logging
    def _parse_entry_xml(
        self, entry: ElementTree.Element
    ) -> List[JMdictEntry]:
        """Parse all elements from a given JMdict XML entry.

        Because many Japanese words can be written using kanji as well as kana,
        there are often different ways to write the same word. JMdict entries
        include each of these representations as separate elements, so this
        function parses all of these elements plus the corresponding sense
        information and merges the info together into JMdictEntry objects.

        Args:
            entry: An XML entry element from a JMdict XML file.

        Returns:
            A list of all of the elements for the given entry.

        Raises:
            ResourceLoadError: The passed entry had malformed JMdict XML, so it
            could not be parsed.
        """
        repr_objs = []
        sense_objs = []
        for element in entry:
            if element.tag in self._REPR_ELEMENT_TAGS:
                repr_obj = JMdictEntry()
                self._parse_text_elements(
                    repr_obj, entry, [self._ENTRY_ID_TAG], required=True
                )
                self._parse_text_elements(
                    repr_obj, element, [self._REPR_TEXT_FORM_TAG[element.tag]],
                    required=True
                )
                self._parse_text_elements(
                    repr_obj, element, self._REPR_OPTIONAL_TAGS[element.tag],
                    required=False
                )
                repr_objs.append(repr_obj)

            elif element.tag == self._SENSE_ELEMENT_TAG:
                sense_obj = self._JMdictSense()
                self._parse_text_elements(
                    sense_obj, element, self._SENSE_OPTIONAL_TAGS,
                    required=False
                )
                sense_objs.append(sense_obj)

            elif element.tag != self._ENTRY_ID_TAG:
                entry_str = ElementTree.tostring(entry).decode('utf-8')
                utils.log_and_raise(
                    _log, ResourceLoadError,
                    'Malformed JMdict XML. Unknown tag "{}" found with "{}" '
                    'tag: "{}"'.format(element.tag, entry.tag, entry_str)
                )

        self._add_sense_data(repr_objs, sense_objs)
        return repr_objs

    @utils.skip_method_debug_logging
    def _add_sense_data(
        self, entries: List[JMdictEntry], senses: List['JMdict._JMdictSense']
    ) -> None:
        """Add the data from the sense objs to the entry objs."""
        for sense in senses:
            for entry in entries:
                if (sense.applicable_elements is not None
                        and len(sense.applicable_elements) > 0
                        and entry.text_form not in sense.applicable_elements):
                    continue

                entry.parts_of_speech = sense.parts_of_speech
                entry.fields = sense.fields
                entry.dialects = sense.dialects
                entry.misc = sense.misc

    @utils.skip_method_debug_logging
    def _parse_text_elements(
        self, storage_obj: Any, parent_element: ElementTree.Element,
        element_tags: List[str], required: bool = False
    ) -> None:
        """Parse text-containing elements and stores the data in a object.

        Args:
            storage_obj: An object with attribute names mapped to by the
                _TAG_TO_OBJ_ATTR_MAP.
            parent_element: The XML element whose children to parse for the
                text-containing elements.
            element_tags: The tags of the elements to parse.
            required: If True, will raise an error if any of the elements are
                not found within the children of the parent element.

        Raises:
            ResourceLoadError: A required element was not found, or an element
                was found with no parsable text within it.
        """
        for tag in element_tags:
            if required:
                elements = self._find_all_raise_if_none(tag, parent_element)
            else:
                elements = parent_element.findall(tag)

            for ele in elements:
                self._raise_if_no_text(ele, parent_element)
                if tag in self._TUPPLE_TAGS:
                    self._append_to_tuple_attr(
                        storage_obj, self._TAG_TO_OBJ_ATTR_MAP[tag], ele.text
                    )
                else:
                    setattr(
                        storage_obj, self._TAG_TO_OBJ_ATTR_MAP[tag], ele.text
                    )

    @utils.skip_method_debug_logging
    def _append_to_tuple_attr(
        self, storage_obj: Any, attr_name: str, append_item: str
    ) -> None:
        """Create new tuple for attr of storage object with item appended."""
        current_val = getattr(storage_obj, attr_name)
        if current_val is None:
            setattr(storage_obj, attr_name, (append_item,))
        else:
            setattr(storage_obj, attr_name, current_val + (append_item,))

    @utils.skip_method_debug_logging
    def _find_all_raise_if_none(
        self, tag: str, parent_element: ElementTree.Element
    ) -> List[ElementTree.Element]:
        """Find all tag elements in parent, and raises error if none.

        Raises ResourceLoadError if no tag elements are found.
        """
        elements = parent_element.findall(tag)
        if len(elements) == 0:
            parent_str = ElementTree.tostring(parent_element).decode('utf-8')
            utils.log_and_raise(
                _log, ResourceLoadError,
                'Malformed JMdict XML. No "{}" element within "{}" element: '
                '"{}"'.format(tag, parent_element.tag, parent_str)
            )

        return elements

    @utils.skip_method_debug_logging
    def _raise_if_no_text(
        self, element: ElementTree.Element, parent_element: ElementTree.Element
    ) -> None:
        """Raise ResourceLoadError if no accessible text in element."""
        if element.text is not None and len(element.text) > 0:
            return

        parent_str = ElementTree.tostring(parent_element).decode('utf-8')
        utils.log_and_raise(
            _log, ResourceLoadError,
            'Malformed JMdict XML. No accessible text within "{}" element: '
            '"{}"'.format(element.tag, parent_str)
        )

    def load_jmdict(self, xml_filepath: str) -> None:
        """Load data from a JMdict XML file.

        Args:
            xml_filepath: Path to an JMdict XML file.

        Raises:
            ResourceLoadError: There was an issue with the passed JMdict XML
                file that prevented it from being loaded.
        """
        xml_last_modified_time = os.path.getmtime(xml_filepath)
        if self._load_from_shelf_if_newer(xml_last_modified_time):
            return

        if not os.path.exists(xml_filepath):
            utils.log_and_raise(
                _log, ResourceLoadError,
                'JMdict file not found at "{}"'.format(xml_filepath)
            )

        _log.debug('Reading JMdict XML file at "%s"', xml_filepath)
        tree = ElementTree.parse(xml_filepath)
        _log.debug('Reading of JMdict XML file complete')

        self._entry_map = defaultdict(list)
        self._mecab_decomp_map = defaultdict(list)
        root = tree.getroot()
        for entry_element in root:
            entry_objs = self._parse_entry_xml(entry_element)
            for entry_obj in entry_objs:
                mecab_decomp = self._get_mecab_decomb(entry_obj)
                self._mecab_decomp_map[mecab_decomp].append(entry_obj)
                self._entry_map[entry_obj.text_form].append(entry_obj)

        self._set_max_entry_lens()
        self._write_to_shelf()

    @utils.skip_method_debug_logging
    def _get_mecab_decomb(self, entry: JMdictEntry) -> Tuple[str, ...]:
        """Get the MeCab decomposition of the text form of the entry."""
        flis = self._mecab_tagger.parse(entry.text_form)
        base_forms = [item.base_form for item in flis]
        return tuple(base_forms)

    def _set_max_entry_lens(self) -> None:
        """Set the properties for max entry lengths."""
        self._max_text_form_len = max(
            len(text_form) for text_form in self._entry_map.keys()
        )
        self._max_mecab_decomp_len = max(
            len(decomp) for decomp in self._mecab_decomp_map.keys()
        )

    def _get_shelf_filepath(self) -> str:
        """Return the file path used for the JMdict shelf."""
        shelf_dir = utils.get_value_from_env_variable(
            myaku.APP_DATA_DIR_ENV_VAR
        )

        return os.path.join(shelf_dir, self._SHELF_FILENAME)

    def _load_from_shelf_if_newer(self, comp_timestamp: float) -> bool:
        """Load JMdict maps from shelf if shelf is newer than given timestamp.

        If the shelf file for JMdict exists and was last modified after the
        given timestamp, loads JMdict data from the shelf.

        Args:
            comp_timestamp: Unix timestamp to compare the shelf last modified
                time against to see if the shelf is newer.

        Returns:
            True if the shelf file was newer and JMdict data was loaded from
            the shelf, or False if the shelf did not exist or was older and no
            JMdict data was loaded.
        """
        shelf_path = self._get_shelf_filepath()
        if not os.path.exists(shelf_path):
            _log.debug(
                'Shelf file does not exist at "%s", so no data loaded from '
                'the shelf', shelf_path
            )
            return False

        shelf_timestamp = os.path.getmtime(shelf_path)
        shelf_dt_timestamp = datetime.utcfromtimestamp(shelf_timestamp)
        comp_dt_timestamp = datetime.utcfromtimestamp(comp_timestamp)
        if shelf_timestamp <= comp_timestamp:
            _log.debug(
                'Shelf file (%s) last mod time (%s) is before or equal to '
                'compare last mod time (%s), so no data loaded from the shelf',
                shelf_path, shelf_dt_timestamp.isoformat(),
                comp_dt_timestamp.isoformat()
            )
            return False

        _log.debug(
            'Shelf file (%s) last mod time (%s) is after compare last mod '
            'time (%s), so loading data from the shelf',
            shelf_path, shelf_dt_timestamp.isoformat(),
            comp_dt_timestamp.isoformat()
        )
        with shelve.open(shelf_path, 'r') as shelf:
            self._entry_map = defaultdict(list)
            self._mecab_decomp_map = {}

            mecab_decomp_map_items = shelf['_mecab_decomp_map_items']
            for mecab_decomp, entry_list in mecab_decomp_map_items:
                self._mecab_decomp_map[mecab_decomp] = entry_list
                for entry in entry_list:
                    self._entry_map[entry.text_form].append(entry)

        self._set_max_entry_lens()
        return True

    def _write_to_shelf(self) -> None:
        """Write current JMdict objects to shelf."""
        shelf_path = self._get_shelf_filepath()
        _log.debug(
            'Writing current JMdict maps to shelf at "%s"', shelf_path
        )
        with shelve.open(shelf_path, 'n') as shelf:
            shelf['_mecab_decomp_map_items'] = list(
                self._mecab_decomp_map.items()
            )

    def contains_entry(self, entry: Union[str, Tuple[str, ...]]) -> bool:
        """Test if entry is in the JMdict entries.

        Args:
            entry: value to check for in the loaded JMdict entries. If a
                string, checks if an entry with that text form exists. If a
                tuple, checks if an entry with that Mecab decomposition exists.

        Returns:
            True if the entry is in the loaded JMdict entries, False otherwise.

        Raises:
            ResourceNotReadyError: JMdict data has not been loaded into this
                JMdict object yet.
        """
        if self._entry_map is None or self._mecab_decomp_map is None:
            utils.log_and_raise(
                _log, ResourceNotReadyError,
                'JMdict object used before loading any JMdict data.'
            )

        if isinstance(entry, str):
            return entry in self._entry_map
        return entry in self._mecab_decomp_map

    def __contains__(self, entry: Union[str, Tuple[str, ...]]) -> bool:
        """Simply call self.contains_entry."""
        return self.contains_entry(entry)

    @utils.skip_method_debug_logging
    def get_entries(
        self, entry: Union[str, Tuple[str, ...]]
    ) -> List[JMdictEntry]:
        """Get the list of JMdict entries that match the give entry.

        Args:
            entry: value to get matching JMdict entries for. If a string, gets
                entries with matching text form. If a tuple, gets entries with
                matching Mecab decomposition.

        Returns:
            A list of the matching JMdict entries.

        Raises:
            ResourceNotReadyError: JMdict data has not been loaded into this
                JMdict object yet.
        """
        if self._entry_map is None or self._mecab_decomp_map is None:
            utils.log_and_raise(
                _log, ResourceNotReadyError,
                'JMdict object used before loading any JMdict data.'
            )

        if isinstance(entry, str):
            return self._entry_map.get(entry, [])
        return self._mecab_decomp_map.get(entry, [])

    @utils.skip_method_debug_logging
    def __getitem__(
        self, entry: Union[str, Tuple[str, ...]]
    ) -> List[JMdictEntry]:
        """Simply call self.get_entries."""
        return self.get_entries(entry)


@utils.singleton_per_config
class MecabTagger:
    """Object representation of a MeCab tagger.

    mecab-python3 provides a wrapper for MeCab in the MeCab module, but that
    wrapper doesn't handle many things such as configuring MeCab settings or
    parsing MeCab tagger output, so this class builds on top of that wrapper to
    make the MeCab tagger easier to work with in Python.
    """
    _MECAB_NEOLOGD_DIR_NAME = 'mecab-ipadic-neologd'
    _END_OF_SECTION_MARKER = 'EOS'
    _POS_SPLITTER = '-'
    _TOKEN_SPLITTER = '\t'
    _EXPECTED_TOKEN_TAG_COUNTS = {4, 5, 6}

    _ADJUST_TAGS_MAP: Dict[MecabTags, MecabTags] = {
        # MeCab tags a single な character with the base form だ, which is
        # technically correct, but in the vast majority of cases, it works
        # better for lexical analysis to treat it as having the base form な.
        ('な', 'ナ', 'だ', '助動詞', '特殊・ダ', '体言接続'):
            ('な', 'ナ', 'な', '助動詞', '特殊・ダ', '体言接続'),
    }

    def __init__(self, use_default_ipadic: bool = False) -> None:
        """Init the MeCab tagger wrapper.

        Unless use_default_ipadic is True, uses the ipadic-NEologd dictionary
        and will raise a ResourceLoadError if NEologd is not available on the
        system.

        Args:
            use_default_ipadic: If True, forces tagger to use the default
                ipadic dictionary instead of the ipadic-NEologd dictionary.
        """
        self._mecab_tagger = None

        # The "-Ochasen" arg here is specifying the output type for MeCab to
        # use. The chasen output type is used because it includes the important
        # tags for each token and is easy to parse.
        if use_default_ipadic:
            self._mecab_tagger = MeCab.Tagger('-Ochasen')
        else:
            neologd_path = self._get_mecab_neologd_dict_path()
            self._mecab_tagger = MeCab.Tagger(f'-Ochasen -d {neologd_path}')

    def parse(
        self, text: str, text_offset: int = 0
    ) -> List[FoundJpnLexicalItem]:
        """Return the lexical items found by MeCab in the text.

        MeCab will give exactly one lexical item interpretation (its best
        guess) for each lexical item found in the text.

        Args:
            text: The text to parse with MeCab for lexical items.
            text_offset: Offset that this text starts at if the text is
                part of a larger body of text.

        Raises:
            TextAnalysisError: MeCab gave an unexpected output when parsing the
                text.
        """
        mecab_out = self._mecab_tagger.parse(text)
        parsed_tokens = self._parse_mecab_output(mecab_out)

        offset = 0
        found_lexical_items = []
        for parsed_token_tags in parsed_tokens:
            if (len(parsed_token_tags) == 1
                    and parsed_token_tags[0] == self._END_OF_SECTION_MARKER):
                continue

            if len(parsed_token_tags) not in self._EXPECTED_TOKEN_TAG_COUNTS:
                utils.log_and_raise(
                    _log, TextAnalysisError,
                    'Unexpected number of MeCab tags ({}) for token {} in '
                    '"{}"'.format(
                        len(parsed_token_tags), parsed_token_tags, text
                    )
                )

            # Adjust offset to account for MeCab skipping some white space
            # characters.
            while (text[offset:offset + len(parsed_token_tags[0])]
                   != parsed_token_tags[0]):
                offset += 1

            interp = self._create_mecab_interp(parsed_token_tags)
            fli = self._create_found_lexical_item(
                parsed_token_tags, interp, text_offset + offset
            )
            offset += len(parsed_token_tags[0])
            found_lexical_items.append(fli)

        return found_lexical_items

    def _parse_mecab_output(self, output: str) -> List[List[str]]:
        """Parse the individual tags from MeCab chasen output.

        Adjusts some values from the output if they are known problems.

        Args:
            output: MeCab chasen output.

        Returns:
            A list where each entry is a list of the tokens parsed from one
            line of the output.
        """
        parsed_tokens = []
        for line in output.splitlines():
            if len(line) == 0:
                continue
            tags = line.split(self._TOKEN_SPLITTER)
            self._adjust_if_known_problem(tags)

            tags = [t for t in tags if len(t) > 0]
            parsed_tokens.append(tags)

        return parsed_tokens

    def _adjust_if_known_problem(self, tags: List[str]) -> None:
        """Adjust tags for token if known MeCab parsing problem.

        MeCab parses some Japanese tokens in ways that cause problems for
        finding lexical items. This function adjusts tags in these cases to fix
        these problems.

        Args:
            tags: A list of tags parsed from a line of MeCab output. The tags
                will be adjusted in place in the list.
        """
        # Very rarely, MeCab will give a blank base form for some proper
        # nouns. In these cases, set the base form to be the same as the
        # surface form.
        if len(tags) >= 4 and len(tags[0]) > 0 and len(tags[2]) == 0:
            tags[2] = tags[0]

        adjusted_tags = self._ADJUST_TAGS_MAP.get(tuple(tags))
        if adjusted_tags is not None:
            for i, _ in enumerate(tags):
                tags[i] = adjusted_tags[i]

    def _create_mecab_interp(
        self, parsed_token_tags: List[str]
    ) -> JpnLexicalItemInterp:
        """Create a MecabLexicalItemInterp from the parsed token tags."""
        parts_of_speech = tuple(
            parsed_token_tags[3].split(self._POS_SPLITTER)
        )
        if len(parsed_token_tags) >= 6:
            mecab_interp = MecabLexicalItemInterp(
                parts_of_speech=parts_of_speech,
                conjugated_type=parsed_token_tags[4],
                conjugated_form=parsed_token_tags[5]
            )
        if len(parsed_token_tags) >= 5:
            mecab_interp = MecabLexicalItemInterp(
                parts_of_speech=parts_of_speech,
                conjugated_type=parsed_token_tags[4]
            )
        else:
            mecab_interp = MecabLexicalItemInterp(
                parts_of_speech=parts_of_speech
            )

        return JpnLexicalItemInterp(
            mecab_interp=mecab_interp,
            interp_sources=(InterpSource.MECAB,)
        )

    def _create_found_lexical_item(
        self, parsed_token_tags: List[str], interp: JpnLexicalItemInterp,
        offset: int
    ) -> FoundJpnLexicalItem:
        """Create a found lexical item from the tags, interp, and offset."""
        found_lexical_item = FoundJpnLexicalItem(
            base_form=parsed_token_tags[2],
            found_positions=[ArticleTextPosition(
                offset, len(parsed_token_tags[0])
            )],
            possible_interps=[interp]
        )
        found_lexical_item.cache_surface_form(
            parsed_token_tags[0],
            found_lexical_item.found_positions[0]
        )

        return found_lexical_item

    def _get_mecab_neologd_dict_path(self) -> str:
        """Find the path to the NEologd dict in the system.

        Returns:
            The path to the directory containing the NEologd dictionary.
        """
        output = subprocess.run(
            ['mecab-config', '--version'], capture_output=True
        )
        if output.returncode != 0:
            utils.log_and_raise(
                _log, ResourceLoadError,
                'MeCab is not installed on this system, so the '
                'mecab-ipadic-NEologd dictionary cannot be used'
            )

        output = subprocess.run(
            ['mecab-config', '--dicdir'], capture_output=True
        )
        if output.returncode != 0:
            utils.log_and_raise(
                _log, ResourceLoadError,
                'MeCab dictionary directory could not be retrieved, so the '
                'mecab-ipadic-NEologd dictionary cannot be used'
            )

        neologd_path = os.path.join(
            output.stdout.decode(sys.stdout.encoding).strip(),
            self._MECAB_NEOLOGD_DIR_NAME
        )
        if not os.path.exists(neologd_path):
            utils.log_and_raise(
                _log, ResourceLoadError,
                'mecab-ipadic-NEologd is not installed on this system, so the '
                'mecab-ipadic-NEologd dictionary cannot be used'
            )

        return neologd_path
