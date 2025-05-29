// Copyright (c) 2025 Computer Vision Center (CVC) at the Universitat Autonoma
// de Barcelona (UAB).
//
// This work is licensed under the terms of the MIT license.
// For a copy, see <https://opensource.org/licenses/MIT>.

#pragma once

#include "CoreMinimal.h"
#include "Widgets/SCompoundWidget.h"
#include "TrafficLights/Widgets/STrafficLightPreviewViewport.h"
#include "TrafficLights/TLHead.h"
#include "TrafficLights/TLHeadOrientation.h"
#include "TrafficLights/TLHeadAttachment.h"
#include "TrafficLights/TLHeadStyle.h"

class STrafficLightToolWidget : public SCompoundWidget
{
public:
    SLATE_BEGIN_ARGS(STrafficLightToolWidget) {}
    SLATE_END_ARGS()

    /** Construct tool widget */
    void Construct(const FArguments& InArgs);

    /** HEAD */
private:
    TSharedRef<SWidget> BuildHeadEntry(int32 HeadIndex);
    /** Add a new head */
    FReply OnAddHeadClicked();
    /** Delete existing head by index */
    FReply OnDeleteHeadClicked(int32 Index);
    /** Rebuild the UI list */
    void RefreshHeadList();
    /** Create a single head entry */
    TSharedRef<SWidget> CreateHeadEntry(int32 Index);

    /** MODULE */
private:
    TSharedRef<SWidget> BuildModuleEntry(int32 HeadIndex, int32 ModuleIndex);
    TSharedRef<SWidget> BuildModulesSection(int32 HeadIndex);
    /** Add a new module */
    FReply OnAddModuleClicked(int32 HeadIndex);
    TSharedRef<SWidget> CreateModuleEntry(int32 HeadIndex, int32 ModuleIndex);
    /** Delete existing head by index */
    FReply OnDeleteModuleClicked(int32 HeadIndex, int32 ModuleIndex);
    void UpdateModuleMeshesInViewport(int32 HeadIndex);


private:
    FString GetHeadStyleText(ETLHeadStyle Style);
    FString GetHeadAttachmentText(ETLHeadAttachment Attach);
    FString GetHeadOrientationText(ETLHeadOrientation Orient);
    FString GetLightTypeText(ETLLightType Type);

    /** Head option values */
private:
    TArray<TSharedPtr<FString>> HeadStyleOptions;
    TArray<TSharedPtr<FString>> HeadOrientationOptions;
    TArray<TSharedPtr<FString>> HeadAttachmentOptions;
    TArray<TSharedPtr<FString>> LightTypeOptions;

private:
    TSharedPtr<STrafficLightPreviewViewport> PreviewViewport;
    TSharedPtr<SVerticalBox> HeadListContainer;
    TArray<TSharedPtr<SVerticalBox>> ModuleListContainers;
    TArray<FTLHead> Heads;
};
