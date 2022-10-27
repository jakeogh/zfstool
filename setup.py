# -*- coding: utf-8 -*-
from setuptools import find_packages
from setuptools import setup

import fastentrypoints

dependencies = ["click"]

config = {
    "version": "0.1",
    "name": "zfstool",
    "url": "https://github.com/jakeogh/zfstool",
    "license": "ISC",
    "author": "Justin Keogh",
    "author_email": "github.com@v6y.net",
    "description": "Common functions for working with zfs",
    "long_description": __doc__,
    "packages": find_packages(exclude=["tests"]),
    "package_data": {"zfstool": ["py.typed"]},
    "include_package_data": True,
    "zip_safe": False,
    "platforms": "any",
    "install_requires": dependencies,
    "entry_points": {
        "console_scripts": [
            "zfstool=zfstool.zfstool:cli",
        ],
    },
}

setup(**config)
