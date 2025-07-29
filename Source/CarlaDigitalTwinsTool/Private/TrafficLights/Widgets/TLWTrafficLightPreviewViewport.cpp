#include "TrafficLights/Widgets/TLWTrafficLightPreviewViewport.h"

#include "Components/StaticMeshComponent.h"
#include "Logging/LogMacros.h"
#include "Math/MathFwd.h"
#include "UObject/UObjectGlobals.h"

void STrafficLightPreviewViewport::Construct(const FArguments& InArgs)
{
	PreviewScene = MakeUnique<FPreviewScene>(FPreviewScene::ConstructionValues());

	{
		UWorld* PreviewWorld = PreviewScene->GetWorld();
		if (PreviewWorld)
		{
			FActorSpawnParameters SpawnParams;
			SpawnParams.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
			SpawnParams.ObjectFlags |= RF_Transient;
			PreviewTrafficLight =
				PreviewWorld->SpawnActor<ATrafficLightActor>(ATrafficLightActor::StaticClass(), FTransform::Identity, SpawnParams);
		}
	}

	ViewportClient = MakeShareable(new FEditorViewportClient(nullptr, PreviewScene.Get(), nullptr));
	ViewportClient->bSetListenerPosition = false;
	ViewportClient->SetRealtime(false);
	ViewportClient->SetViewLocation(FVector(-300, 0, 150));
	ViewportClient->SetViewRotation(FRotator(0, 0, 0));
	ViewportClient->SetViewMode(VMI_Lit);
	ViewportClient->SetAllowCinematicControl(true);
	ViewportClient->VisibilityDelegate.BindLambda([]() { return true; });
	ViewportClient->EngineShowFlags.SetGrid(true);

	SAssignNew(ViewportWidget, SViewport).EnableGammaCorrection(false).EnableBlending(true);

	SceneViewport = MakeShareable(new FSceneViewport(ViewportClient.Get(), ViewportWidget));
	ViewportClient->Viewport = SceneViewport.Get();
	ViewportWidget->SetViewportInterface(SceneViewport.ToSharedRef());

	ChildSlot[ViewportWidget.ToSharedRef()];
}

STrafficLightPreviewViewport::~STrafficLightPreviewViewport()
{
	if (PreviewTrafficLight)
	{
		PreviewTrafficLight->Destroy();
		PreviewTrafficLight = nullptr;
	}
	if (ViewportClient.IsValid())
	{
		ViewportClient->Viewport = nullptr;
		FlushRenderingCommands();
		PreviewScene.Reset();
	}
}

void STrafficLightPreviewViewport::Reload()
{
	if (SceneViewport.IsValid())
	{
		SceneViewport->Invalidate();
	}
	if (ViewportClient.IsValid())
	{
		ViewportClient->Invalidate();
	}
}

void STrafficLightPreviewViewport::ResetFrame()
{
	check(IsValid(PreviewTrafficLight));
	check(ViewportClient.IsValid());

	const FBox Bounds{PreviewTrafficLight->GetComponentsBoundingBox(true)};
	const FVector Center{Bounds.GetCenter()};
	const double Radius{Bounds.GetExtent().GetMax()};
	const double Distance{Radius * -10.0};
	const FVector Forward{FVector::ForwardVector.Rotation().RotateVector(FVector(0, 1, 0))};
	const FVector Up{FVector::UpVector};
	const FVector CamPos{Center - Forward * Distance + Up * (Radius * 0.5)};
	const FRotator CamRot(0.0, -90.0, 0.0);

	ViewportClient->SetViewLocation(CamPos);
	ViewportClient->SetViewRotation(CamRot);
}
