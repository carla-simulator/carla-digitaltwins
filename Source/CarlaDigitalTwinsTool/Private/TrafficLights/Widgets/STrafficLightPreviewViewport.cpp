#include "TrafficLights/Widgets/STrafficLightPreviewViewport.h"
#include "Engine/StaticMesh.h"
#include "Components/StaticMeshComponent.h"
#include "Materials/MaterialInterface.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "TrafficLights/TLHead.h"
#include "UObject/UObjectGlobals.h"


void STrafficLightPreviewViewport::Construct(const FArguments& InArgs)
{
    CubeMesh = LoadObject<UStaticMesh>(
        nullptr,
        TEXT("/Engine/BasicShapes/Cube.Cube")
    );

    BaseMaterial = LoadObject<UMaterialInterface>(
        nullptr,
        TEXT("/Game/Materials/M_DynamicColor.M_DynamicColor")
    );

    PreviewScene = MakeUnique<FPreviewScene>(FPreviewScene::ConstructionValues());

    ViewportClient = MakeShareable(new FEditorViewportClient(nullptr, PreviewScene.Get(), nullptr));
    ViewportClient->bSetListenerPosition = false;
    ViewportClient->SetRealtime(true);
    ViewportClient->SetViewLocation(FVector(-300, 0, 150));
    ViewportClient->SetViewRotation(FRotator(-15, 0, 0));
    ViewportClient->SetViewMode(VMI_Lit);
    ViewportClient->SetAllowCinematicControl(true);
    ViewportClient->VisibilityDelegate.BindLambda([]() { return true; });
    ViewportClient->EngineShowFlags.SetGrid(true);

    SAssignNew(ViewportWidget, SViewport)
        .EnableGammaCorrection(false)
        .EnableBlending(true);

    SceneViewport = MakeShareable(new FSceneViewport(ViewportClient.Get(), ViewportWidget));
    ViewportClient->Viewport = SceneViewport.Get();
    ViewportWidget->SetViewportInterface(SceneViewport.ToSharedRef());

    ChildSlot
    [
        ViewportWidget.ToSharedRef()
    ];
}

STrafficLightPreviewViewport::~STrafficLightPreviewViewport()
{
    if (ViewportClient.IsValid())
    {
        ViewportClient->Viewport = nullptr;
    }
}

void STrafficLightPreviewViewport::SetHeadStyle(int32 Index, ETLHeadStyle Style)
{
}

void STrafficLightPreviewViewport::AddBackplateMesh(int32 HeadIndex)
{
    if (!HeadMeshComponents.IsValidIndex(HeadIndex))
        return;

    if (BackplateMeshComponents.IsValidIndex(HeadIndex) &&
        BackplateMeshComponents[HeadIndex] != nullptr)
    {
        return;
    }

    const FVector HeadLoc = HeadMeshComponents[HeadIndex]->GetComponentLocation();
    const FVector BackplateLoc = HeadLoc + FVector(10.f, 0.f, 0.f);

    UStaticMeshComponent* Comp = NewObject<UStaticMeshComponent>(
        PreviewScene->GetWorld()->GetCurrentLevel(), NAME_None, RF_Transient
    );
    Comp->SetStaticMesh(
        Cast<UStaticMesh>(StaticLoadObject(
            UStaticMesh::StaticClass(), nullptr,
            TEXT("/Engine/BasicShapes/Cube.Cube")
        ))
    );
    Comp->SetWorldScale3D(FVector(0.1f, 1.2f, 1.2f));
    Comp->SetWorldLocation(BackplateLoc);

    Comp->RegisterComponentWithWorld(PreviewScene->GetWorld());
    Comp->AttachToComponent(
        HeadMeshComponents[HeadIndex],
        FAttachmentTransformRules::KeepWorldTransform
    );

    if (BackplateMeshComponents.Num() <= HeadIndex)
    {
        BackplateMeshComponents.SetNum(HeadMeshComponents.Num());
    }
    BackplateMeshComponents[HeadIndex] = Comp;
}

void STrafficLightPreviewViewport::RemoveBackplateMesh(int32 HeadIndex)
{
    if (!BackplateMeshComponents.IsValidIndex(HeadIndex))
        return;

    if (UStaticMeshComponent* Comp = BackplateMeshComponents[HeadIndex])
    {
        Comp->DestroyComponent();
        BackplateMeshComponents[HeadIndex] = nullptr;
    }
}

FLinearColor STrafficLightPreviewViewport::InitialColorFor(ETLHeadStyle Style) const
{
    switch (Style)
    {
        case ETLHeadStyle::NorthAmerican:
            return FLinearColor::Black;
        case ETLHeadStyle::European:
            return FLinearColor::Blue;
        case ETLHeadStyle::Asian:
            return FLinearColor::White;
        case ETLHeadStyle::Custom:
        default:
            return FLinearColor(1,0,1); // Magenta for custom
    }
}

UStaticMeshComponent* STrafficLightPreviewViewport::AddModuleMesh(const FTLHead& Head, const FTLModule& ModuleData)
{
    const FTransform ModuleWorldTransform {ModuleData.Offset * Head.Transform};

    UStaticMeshComponent* Comp {NewObject<UStaticMeshComponent>(PreviewScene->GetWorld()->GetCurrentLevel())};
    if (!Comp)
    {
        UE_LOG(LogTemp, Error, TEXT("Failed to create StaticMeshComponent for module"));
        return nullptr;
    }

    Comp->RegisterComponentWithWorld(PreviewScene->GetWorld());
    Comp->SetStaticMesh(CubeMesh);
    Comp->SetWorldLocation(ModuleWorldTransform.GetLocation());
    Comp->SetWorldRotation(ModuleWorldTransform.GetRotation());
    Comp->SetWorldScale3D(ModuleWorldTransform.GetScale3D());

    // TODO: Remove this when we have proper assets for each module
    if (BaseMaterial)
    {
        UMaterialInstanceDynamic* Dyn {UMaterialInstanceDynamic::Create(BaseMaterial, Comp)};
        FLinearColor C {FLinearColor::White};
        switch (ModuleData.LightType)
        {
            case ETLLightType::Red:    C = FLinearColor::Red;    break;
            case ETLLightType::Yellow: C = FLinearColor::Yellow; break;
            case ETLLightType::Green:  C = FLinearColor::Green;  break;
            default:                   C = FLinearColor::Gray;   break;
        }
        Dyn->SetVectorParameterValue(TEXT("Color"), C);
        Comp->SetMaterial(0, Dyn);
    }

    ModuleMeshComponents.Add(Comp);
    return Comp;
}

void STrafficLightPreviewViewport::RemoveModuleMeshesForHead(int32 HeadIndex)
{
    for (UStaticMeshComponent* Comp : ModuleMeshComponents)
    {
        if (Comp)
        {
            Comp->DestroyComponent();
        }
    }
    ModuleMeshComponents.Empty();
}

void STrafficLightPreviewViewport::RemoveModuleMeshForHead(int32 HeadIndex, int32 ModuleIndex)
{
    check(ModuleMeshComponents.IsValidIndex(ModuleIndex));
    UStaticMeshComponent* Comp {ModuleMeshComponents[ModuleIndex]};
    if (!Comp)
    {
        UE_LOG(LogTemp, Warning, TEXT("RemoveModuleMeshForHead: Invalid module index %d"), ModuleIndex);
        ModuleMeshComponents.RemoveAt(ModuleIndex);
        return;
    }
    Comp->DestroyComponent();
    ModuleMeshComponents.RemoveAt(ModuleIndex);
}
