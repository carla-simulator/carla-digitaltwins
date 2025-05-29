// Copyright Epic Games, Inc. All Rights Reserved.

#pragma once

#include "Modules/ModuleManager.h"

DECLARE_LOG_CATEGORY_EXTERN(LogCarlaDigitalTwinsTool, Log, All);

class FCarlaDigitalTwinsToolModule : public IModuleInterface
{
public:

	/** IModuleInterface implementation */
	virtual void StartupModule() override;
	virtual void ShutdownModule() override;

private:
	void AddMenuEntry(class FMenuBuilder& Builder);
	void OpenTrafficLightToolTab();
};
