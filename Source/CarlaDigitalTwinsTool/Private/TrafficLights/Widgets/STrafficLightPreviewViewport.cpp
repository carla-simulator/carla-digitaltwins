#include "TrafficLights/Widgets/STrafficLightPreviewViewport.h"
#include "Engine/StaticMesh.h"
#include "Components/StaticMeshComponent.h"
#include "Materials/MaterialInterface.h"
#include "Materials/MaterialInstanceDynamic.h"
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

UStaticMeshComponent* STrafficLightPreviewViewport::AddHeadMesh(const FVector& Location, ETLHeadStyle Style)
{
    if (!PreviewScene.IsValid())
    {
        UE_LOG(LogTemp, Error, TEXT("Preview Scene is not valid"));
        return nullptr;
    }

    if (!CubeMesh)
    {
        UE_LOG(LogTemp, Error, TEXT("Failed to load Cube Mesh"));
        return nullptr;
    }

    UStaticMeshComponent* Comp {NewObject<UStaticMeshComponent>(PreviewScene->GetWorld()->GetCurrentLevel())};
    Comp->RegisterComponentWithWorld(PreviewScene->GetWorld());
    Comp->SetStaticMesh(CubeMesh);

    if (BaseMaterial)
    {
        UMaterialInstanceDynamic* DynMat =
            UMaterialInstanceDynamic::Create(BaseMaterial, Comp);
        DynMat->SetVectorParameterValue(
            TEXT("Color"),
            InitialColorFor(Style)
        );
        Comp->SetMaterial(0, DynMat);
        HeadDynMats.Add(DynMat);
    }

    Comp->SetRelativeLocation(Location);

    HeadMeshComponents.Add(Comp);
    return Comp;
}

void STrafficLightPreviewViewport::SetHeadStyle(int32 Index, ETLHeadStyle Style)
{
    if (!HeadMeshComponents.IsValidIndex(Index))
    {
        UE_LOG(LogTemp, Warning, TEXT("Invalid head index: %d"), Index);
        return;
    }

    UStaticMeshComponent* Comp {HeadMeshComponents[Index]};
    if (!Comp)
    {
        UE_LOG(LogTemp, Warning, TEXT("No mesh component found for head index: %d"), Index);
        return;
    }

    auto* DynMat = Cast<UMaterialInstanceDynamic>(Comp->GetMaterial(0));
    if (!DynMat)
    {
        UE_LOG(LogTemp, Warning, TEXT("No dynamic material instance found for head index: %d"), Index);
        return;
    }

    UStaticMesh* NewMesh = LoadMeshForStyle(Style);
    if (!NewMesh)
    {
        UE_LOG(LogTemp, Error, TEXT("Failed to load mesh for style: %d"), (int32)Style);
        return;
    }
    Comp->SetStaticMesh(NewMesh);
    // TODO: Remove colors when the have a proper asset for each style
    FLinearColor C{InitialColorFor(Style)};
    DynMat->SetVectorParameterValue("Color", C);
    Comp->MarkRenderStateDirty();
}

void STrafficLightPreviewViewport::RemoveHeadMesh(int32 Index)
{
    if (HeadMeshComponents.IsValidIndex(Index))
    {
        UStaticMeshComponent* MeshComp {HeadMeshComponents[Index]};
        if (MeshComp)
        {
            PreviewScene->RemoveComponent(MeshComp);
            MeshComp->DestroyComponent();
        }
        HeadMeshComponents.RemoveAt(Index);
    }
}

void STrafficLightPreviewViewport::UpdateMeshTransforms(const TArray<FTLHead>& Heads)
{
    int32 Count {FMath::Min(HeadMeshComponents.Num(), Heads.Num())};
    for (int32 i {0}; i < Count; ++i)
    {
        UStaticMeshComponent* Mesh {HeadMeshComponents[i]};
        if (!Mesh) continue;
        const FTLHead& HeadData {Heads[i]};
        FTransform FinalTransform {HeadData.RelativeTransform};
        FinalTransform.ConcatenateRotation(HeadData.Offset.GetRotation());
        FinalTransform.AddToTranslation(HeadData.Offset.GetLocation());
        FinalTransform.SetScale3D(HeadData.Offset.GetScale3D());
        Mesh->SetRelativeTransform(FinalTransform);
    }
}

UStaticMesh* STrafficLightPreviewViewport::LoadMeshForStyle(ETLHeadStyle Style)
{
    //TODO: This should be configurable or loaded from a data table
    const TCHAR* Paths[] = {
        TEXT("/Engine/BasicShapes/Cube.Cube"),      // NorthAmerican
        TEXT("/Engine/BasicShapes/Cube.Cube"),  // European
        TEXT("/Engine/BasicShapes/Cube.Cube"), // Asian
        TEXT("/Engine/BasicShapes/Cube.Cube")       // Custom
    };
    const int32 idx {(int32)Style};
    if (idx < 0 || idx >= UE_ARRAY_COUNT(Paths))
    {
        UE_LOG(LogTemp, Warning, TEXT("Invalid ETLHeadStyle index: %d"), idx);
        return nullptr;
    }
    return Cast<UStaticMesh>(StaticLoadObject(UStaticMesh::StaticClass(),
                                              nullptr, Paths[idx]));
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

UStaticMeshComponent* STrafficLightPreviewViewport::AddModuleMesh(int32 HeadIndex, const FTLModule& ModuleData)
{
    if (!HeadMeshComponents.IsValidIndex(HeadIndex))
    {
        UE_LOG(LogTemp, Warning, TEXT("Invalid head index: %d"), HeadIndex);
        return nullptr;
    }
    UStaticMeshComponent* HeadComp {HeadMeshComponents[HeadIndex]};
    const FTransform HeadWorldTransform {HeadComp->GetComponentTransform()};
    const FTransform ModuleWorldTransform {ModuleData.Offset * HeadWorldTransform};

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
