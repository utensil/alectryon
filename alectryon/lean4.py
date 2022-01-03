# Copyright © 2021 Niklas Bülow
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import tempfile
import os
from pathlib import Path
from alectryon import json

from alectryon.json import PlainSerializer
from .core import CLIDriver, EncodedDocument, FragmentContent, indent, Text, FragmentToken
from .transforms import transform_contents_to_tokens

class Lean4(CLIDriver):
    BIN = "leanInk"
    NAME = "Lean4"

    VERSION_ARGS = ("lV",)

    ID = "leanInk"
    LANGUAGE = "lean4"

    CLI_ARGS = ("analyze", "--experimental-type-tokens",)

    TMP_PREFIX = "leanInk_"
    LEAN_FILE_EXT = ".lean"
    LEAN_INK_FILE_EXT = ".leanInk"
    LAKE_ENV_KEY = "--lake"
    LAKE_TMP_FILE_PATH = "lakefile.lean"

    def run_leanink_document(self, encoded_document):
        r"""
        Run LeanInk with encoded_document file.
        """
        with tempfile.TemporaryDirectory(prefix=self.TMP_PREFIX) as temp_directory:
            input_file = Path(temp_directory) / os.path.basename(self.fpath.with_suffix(self.LEAN_FILE_EXT))
            input_file.write_bytes(encoded_document.contents)
            working_directory = temp_directory
            if self.lake_file_path is not None:
                working_directory = os.path.dirname(os.path.realpath(self.lake_file_path))
                self.user_args += [self.LAKE_ENV_KEY, self.LAKE_TMP_FILE_PATH]
            self.run_cli(working_directory=working_directory, capture_output=False, more_args=[str(os.path.abspath(input_file))])
            output_file = input_file.with_suffix(self.LEAN_FILE_EXT + self.LEAN_INK_FILE_EXT)
            content = output_file.read_text(encoding="utf-8")
            json_result = json.loads(content)
            tuple_result = PlainSerializer.decode(json_result)
            return tuple_result
    
    def resolve_lake_arg(self):
        r"""
        Remove lake argument from user_args for manual evaluation.
        """
        new_user_args = []
        self.lake_file_path = None
        for (index, arg) in enumerate(self.user_args, start=0):
            if arg == "--lake":
                self.lake_file_path = self.user_args[index + 1]
                new_user_args = self.user_args[index + 2:]
                break
            else:
                new_user_args += arg
        self.user_args = new_user_args

    def annotate(self, chunks):
        document = EncodedDocument(chunks, "\n", encoding="utf-8")
        self.resolve_lake_arg()
        result = transform_contents_to_tokens(self.run_leanink_document(document))

        if not result:
            return list([])
        
        # Sometimes we require an additional \n and sometimes not. I wasn't really able to
        # find out exactly when, but this workaround seems to work for almost all cases.
        if result[-1].contents.endswith("\n"):
            return list(document.recover_chunks(result))
        else:
            result += [Text(contents=FragmentContent.create("\n"))]
            return list(document.recover_chunks(result))