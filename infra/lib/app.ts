#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { buildApp } from "./build-app";

buildApp(new cdk.App());
