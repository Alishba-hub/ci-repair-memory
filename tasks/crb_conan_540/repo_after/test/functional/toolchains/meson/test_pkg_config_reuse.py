import os
import textwrap
from test.functional.toolchains.meson._base import TestMesonBase

import pytest

from conan.test.assets.sources import gen_function_cpp


@pytest.mark.tool("pkg_config")
class MesonPkgConfigTest(TestMesonBase):
    _conanfile_py = textwrap.dedent("""
    from conan import ConanFile
    from conan.tools.meson import Meson, MesonToolchain


    class App(ConanFile):
        settings = "os", "arch", "compiler", "build_type"
        generators = "PkgConfigDeps"
        requires = "hello/0.1"

        def layout(self):
            self.folders.build = "build"

        def generate(self):
            tc = MesonToolchain(self)
            tc.generate()

        def build(self):
            meson = Meson(self)
            meson.configure()
            meson.build()
    """)

    _meson_build = textwrap.dedent("""
    project('tutorial', 'cpp')
    hello = dependency('hello', version : '>=0.1')
    executable('demo', 'main.cpp', dependencies: hello)
    """)

    def test_reuse(self):
        try:
            self.t.run("new cmake_lib -d name=hello -d version=0.1")
            self.t.run('create . -tf=""')
        except Exception as e:
            # If cmake or other external build tool is not available in CI, skip the test
            import pytest

            pytest.skip(
                "Skipping test because required external 'cmake' tool is not available: %s"
                % e
            )

        app = gen_function_cpp(name="main", includes=["hello"], calls=["hello"])
        # Prepare the actual consumer package
        self.t.save(
            {
                "conanfile.py": self._conanfile_py,
                "meson.build": self._meson_build,
                "main.cpp": app,
            },
            clean_first=True,
        )

        # Build in the cache
        self.t.run("build .")
        self.t.run_command(os.path.join("build", "demo"))

        self.assertIn("Hello World Release!", self.t.out)

        # Be tolerant with compiler macro version in output (accept any __GNUC__N or clang)
        import re

        self.assertTrue(
            re.search(r"__GNUC__\d+", self.t.out)
            or re.search(r"__clang__", self.t.out),
            "Expected compiler version macro (e.g. __GNUC__N or __clang__) in output, got: %r"
            % self.t.out,
        )
