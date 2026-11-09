#pragma once

#include <QtQml/QQmlApplicationEngine>

void app_create();
int app_exec();
void app_quit();
void app_destroy();
bool load_main(QQmlApplicationEngine& engine);
