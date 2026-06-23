from setuptools import setup, Extension
from setuptools.command.build_ext import build_ext


class OptionalBuildExt(build_ext):
    """Skip the C extension gracefully if headers or a compiler are missing."""

    def run(self):
        try:
            super().run()
        except Exception as exc:
            print(f"WARNING: C extension build failed ({exc}); "
                  "falling back to pure-Python implementation.")

    def build_extension(self, ext):
        try:
            super().build_extension(ext)
        except Exception as exc:
            print(f"WARNING: Skipping {ext.name}: {exc}")


setup(
    ext_modules=[
        Extension(
            "rmbt_client.rmbt_loop",
            sources=["rmbt_client/rmbt_loop.c"],
        )
    ],
    cmdclass={"build_ext": OptionalBuildExt},
    package_data={"rmbt_client": ["rmbt_loop.c"]},
)
